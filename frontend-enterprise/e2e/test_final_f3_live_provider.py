"""
@Time       : 2026/10/05
@Author     : zhanglp8181
@File       : test_final_f3_live_provider.py
@CallChain  : 正式pytest → 真实独立模拟HTTP → 拒绝/受理实际次数与零效果
@Description: 验证旁证能区分HTTP提交与业务受理，读取不泄密、不写应用或任务成功。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import httpx
import pytest

import final_f3_simulated_provider as provider
from start_final_f3_live_provider import install_http_observation


def test_actual_http_counts_include_rejection_without_business_effect(tmp_path: Path) -> None:
    """实际422和401仍计提交；正确受理无效果，旁证禁止授权和body原文。"""
    runtime = tmp_path / "f3-simulation-http-counts"
    runtime.mkdir(mode=0o700)
    namespace = "f3-sim-" + secrets.token_hex(8)
    key, control = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
                   "F3_SIMULATION_KEY": key, "F3_SIMULATION_CONTROL_KEY": control,
                   "F3_SIMULATION_NAMESPACE": namespace}
    with (runtime / "provider.private.log").open("w") as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("start_final_f3_live_provider.py")),
                                    "--runtime", str(runtime)], env=environment, stdout=log, stderr=log)
        try:
            ready = runtime / "ready.private.json"
            for _ in range(150):
                if ready.is_file() or process.poll() is not None:
                    break
                time.sleep(0.02)
            assert ready.is_file(), "F3_HTTP_COUNT_PROVIDER_READY_REQUIRED"
            port = json.loads(ready.read_text())["port"]
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=3, trust_env=False) as client:
                auth = {"X-Simulation-Key": key}
                assert client.get("/simulation/observations", headers=auth).json()["submit_http_count"] == 0
                body = {"namespace": namespace, "object_id": "f3-obj-" + secrets.token_hex(16),
                        "product_name": "ALPHA91", "quantity": 3, "customer": "SYNTHETIC-F3",
                        "due_date": "2026-10-06"}
                idem = secrets.token_hex(24)
                headers = {**auth, "Idempotency-Key": idem}
                refused = client.post("/simulation/tasks", json={**body, "product_name": "SIMULATED_DECLINE"}, headers=headers)
                assert refused.status_code == 422
                assert client.post("/simulation/tasks", json=body,
                                   headers={**headers, "X-Simulation-Key": "INVALID_SYNTHETIC"}).status_code == 401
                assert client.get("/simulation/objects/" + body["object_id"], headers=auth).status_code == 404
                accepted = client.post("/simulation/tasks", json=body, headers=headers)
                assert accepted.status_code == 202
                observed = client.get("/simulation/observations", headers=auth).json()
                assert observed["submit_http_count"] == 3
                assert [row["status"] for row in observed["rows"] if row["is_submit"]] == [422, 401, 202]
                assert observed["provider_contract"] == "f3-project-provider-v1"
                assert observed["project_provider_contract_pass"] is True
                assert observed["real_vendor_qualified"] is False
                raw = json.dumps(observed) + (runtime / "http-observations.safe.jsonl").read_text()
                assert all(value not in raw for value in (key, control, idem, body["object_id"], namespace))
                assert client.get("/simulation/objects/" + body["object_id"], headers=auth).status_code == 404
                assert client.get("/simulation/observations").status_code == 401
                task = accepted.json()["taskId"]
                assert client.post("/simulation/control/" + task + "/complete", json={},
                                   headers={"X-Simulation-Control-Key": control}).status_code == 200
                polled = client.get("/simulation/tasks/" + task).json()
                effect = client.get("/simulation/objects/" + body["object_id"], headers=auth).json()
                assert polled["result"]["receipt_id"] == effect["receipt_id"]
                assert {name: polled["result"][name] for name in body} == body
                assert effect["effect_count"] == effect["request_count"] == 1
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
    assert process.poll() is not None
    assert stat_private(runtime / "http-observations.safe.jsonl")


def stat_private(path: Path) -> bool:
    """旁证字节只能由本轮拥有者读取，不在公有模式下留下测试授权线索。"""
    return path.stat().st_uid == os.getuid() and path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("outcome", ["accepted", "refused"])
def test_sidecar_write_failure_preserves_original_route_and_marks_evidence_invalid(
    tmp_path: Path, monkeypatch, outcome: str,
) -> None:
    """旁证I/O故障不能把202或原422变成另一业务状态，但缺证据必须粘滞失效。"""
    packet = {"taskId": "synthetic-owned-task", "status": "accepted"}
    refused = provider.SimulationRejected(422, "SIMULATED_SAFE_REJECTION")

    def original_route(_handler):
        """合成控制只提供原对象/异常，不联网或写任何业务效果。"""
        if outcome == "refused":
            raise refused
        return 202, packet

    monkeypatch.setattr(provider.SimulationHandler, "_route", original_route)
    original_open = os.open

    def broken_sidecar(path, *args, **kwargs):
        """只阻断本例旁证文件，保留其他原测试I/O。"""
        if Path(path).name == "http-observations.safe.jsonl":
            raise OSError("SYNTHETIC_SIDECAR_WRITE_FAILURE")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", broken_sidecar)
    install_http_observation(tmp_path)
    handler = SimpleNamespace(command="POST", path="/simulation/tasks",
                              server=SimpleNamespace(drop_next_accept=False), _authorize=lambda: None)
    route = provider.SimulationHandler._route
    if outcome == "refused":
        with pytest.raises(provider.SimulationRejected) as captured:
            route(handler)
        assert captured.value is refused
    else:
        status, returned = route(handler)
        assert status == 202 and returned is packet
    handler.command, handler.path = "GET", "/simulation/observations"
    status, observed = route(handler)
    assert status == 200 and observed["submit_http_count"] == 1
    assert observed["observation_healthy"] is False and observed["observation_error_count"] == 1
    assert observed["provider_contract"] == "f3-project-provider-v1"
    assert observed["project_provider_contract_pass"] is True
    assert observed["real_vendor_qualified"] is False


def test_provider_observation_rows_and_archive_have_a_hard_aggregate_limit(tmp_path, monkeypatch):
    """模拟服务观察列表与JSONL归档超预算后失效，不得继续累计任意多HTTP记录。"""
    runtime = tmp_path / "bounded-provider"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(provider.SimulationHandler, "_route", lambda _handler: (200, {"ok": True}))
    install_http_observation(runtime)
    route = provider.SimulationHandler._route
    request = SimpleNamespace(command="GET", path="/simulation/health",
                               server=SimpleNamespace(drop_next_accept=False))

    for index in range(140):
        if index < 128:
            assert route(request) == (200, {"ok": True})
        else:
            with pytest.raises(provider.SimulationRejected) as rejected:
                route(request)
            assert rejected.value.status == 429

    request.command, request.path, request._authorize = "GET", "/simulation/observations", lambda: None
    status, observation = route(request)
    assert status == 200
    assert len(observation["rows"]) <= 128
    assert observation["observation_healthy"] is False
    assert observation["observation_error_count"] > 0
    archive = runtime / "http-observations.safe.jsonl"
    assert archive.stat().st_size <= 256 * 1024
    assert len(archive.read_text().splitlines()) <= 128


def test_provider_rejects_preexisting_oversized_observation_archive(tmp_path, monkeypatch):
    """已有旁证超过登记字节预算时保留原HTTP结果，但不再扩写并永久失效。"""
    runtime = tmp_path / "oversized-provider"
    runtime.mkdir(mode=0o700)
    archive = runtime / "http-observations.safe.jsonl"
    archive.write_bytes(b"{}\n" * (256 * 1024 // 3 + 1))
    archive.chmod(0o600)
    original_size = archive.stat().st_size
    monkeypatch.setattr(provider.SimulationHandler, "_route", lambda _handler: (200, {"ok": True}))
    install_http_observation(runtime)
    request = SimpleNamespace(command="GET", path="/simulation/health",
                              server=SimpleNamespace(drop_next_accept=False))
    assert provider.SimulationHandler._route(request) == (200, {"ok": True})
    request.command, request.path, request._authorize = "GET", "/simulation/observations", lambda: None
    status, observation = provider.SimulationHandler._route(request)
    assert status == 200 and observation["observation_healthy"] is False
    assert archive.stat().st_size == original_size


def test_callback_uses_actual_submission_token_and_existing_effect_only(tmp_path: Path) -> None:
    """独立服务须用实际提交token向事前目标发回既成效果，pending与错授权不产生回调。"""
    received = []

    class Receiver(BaseHTTPRequestHandler):
        """合成接收端只核真实HTTP字节，不伪造应用Runtime验收。"""

        def do_POST(self) -> None:
            """保留实际传来token与payload供本例私有断言，返回合成202。"""
            value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append({"path": self.path, "token": self.headers.get("X-Gongge-Xuban-External-Task-Token"), "body": value})
            packet = b'{"event_id":"synthetic-inbox"}'
            self.send_response(202)
            self.send_header("Content-Length", str(len(packet)))
            self.end_headers()
            self.wfile.write(packet)

        def log_message(self, _format, *_args) -> None:
            """合成接收不把回调token或正文打印到测试报告。"""

    receiver = HTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    runtime = tmp_path / "f3-simulation-callback"
    runtime.mkdir(mode=0o700)
    key, control, token = (secrets.token_urlsafe(40) for _ in range(3))
    namespace = "f3-sim-" + secrets.token_hex(8)
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
                   "F3_SIMULATION_KEY": key, "F3_SIMULATION_CONTROL_KEY": control,
                   "F3_SIMULATION_NAMESPACE": namespace,
                   "F3_LIVE_APP_ORIGIN": f"http://127.0.0.1:{receiver.server_port}"}
    process = None
    try:
        with (runtime / "provider.private.log").open("w") as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("start_final_f3_live_provider.py")),
                                        "--runtime", str(runtime)], env=environment, stdout=log, stderr=log)
            ready = runtime / "ready.private.json"
            for _ in range(150):
                if ready.is_file() or process.poll() is not None:
                    break
                time.sleep(0.02)
            assert ready.is_file()
            port = json.loads(ready.read_text())["port"]
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=3, trust_env=False) as client:
                business = {"namespace": namespace, "object_id": "f3-obj-" + secrets.token_hex(16),
                            "product_name": "ALPHA91", "quantity": 3, "customer": "SYNTHETIC-F3", "due_date": "2026-10-06"}
                headers = {"X-Simulation-Key": key, "Idempotency-Key": secrets.token_hex(24),
                           "X-Gongge-Xuban-External-Task-Token": token}
                accepted = client.post("/simulation/tasks", json=business, headers=headers)
                assert accepted.status_code == 202
                task = accepted.json()["taskId"]
                callback = {"operation_id": "op-synthetic-owned", "provider_event_id": "event-synthetic-owned", "observation_revision": 1}
                auth = {"X-Simulation-Control-Key": control}
                path = "/simulation/control/" + task + "/callback"
                assert client.post(path, json=callback, headers=auth).status_code == 409
                assert received == []
                assert client.post("/simulation/control/" + task + "/complete", json={}, headers=auth).status_code == 200
                assert (runtime / "callback-bindings.private.json").stat().st_mode & 0o077 == 0
                process.terminate()
                process.wait(timeout=5)
                ready.unlink()
                process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).with_name("start_final_f3_live_provider.py")),
                     "--runtime", str(runtime)], env=environment, stdout=log, stderr=log,
                )
                for _ in range(150):
                    if ready.is_file() or process.poll() is not None:
                        break
                    time.sleep(0.02)
                assert ready.is_file(), "F3_CALLBACK_RESTART_DID_NOT_START"
                client.close()
                port = json.loads(ready.read_text())["port"]
                client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=3, trust_env=False)
                assert client.post(path, json=callback, headers={"X-Simulation-Control-Key": "WRONG_SYNTHETIC"}).status_code == 401
                assert received == []
                sent = client.post(path, json=callback, headers=auth)
                assert sent.status_code == 200 and sent.json()["callback_http_status"] == 202
                assert len(received) == 1 and received[0]["token"] == token
                assert received[0]["path"] == "/api/external-tasks/op-synthetic-owned/callback"
                body = received[0]["body"]
                assert body["provider_task_id"] == task and body["provider_status"] == "completed"
                effect = client.get("/simulation/objects/" + business["object_id"], headers={"X-Simulation-Key": key}).json()
                assert body["payload"]["data"]["receipt_id"] == effect["receipt_id"]
                assert {field: body["payload"]["data"][field] for field in business} == business
                assert token not in (runtime / "http-observations.safe.jsonl").read_text()
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        receiver.shutdown()
        receiver.server_close()
        thread.join(timeout=3)
