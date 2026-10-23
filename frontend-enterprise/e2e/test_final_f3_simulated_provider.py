"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : test_final_f3_simulated_provider.py
@CallChain  : pytest → 自有独立子进程/真实HTTP → 原提交/轮询和独立模拟业务读回
@Description: 锁定受理、幂等、撤销、失联对账与重启合同；不算真实模型或供应商验收。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

import httpx
import pytest


class RemoteSimulation:
    """每例拥有独立服务和私有合成对象，不与应用共享数据库或进程。"""

    def __init__(self, root: Path):
        """生成本例授权和对象身份，秘密不进入命令行或报告。"""
        self.runtime = root / ("f3-simulation-" + secrets.token_hex(8))
        self.runtime.mkdir(mode=0o700)
        self.namespace = "f3-sim-" + secrets.token_hex(8)
        self.key = secrets.token_hex(24)
        self.control_key = secrets.token_hex(24)
        self.body = {"namespace": self.namespace, "object_id": "f3-obj-" + secrets.token_hex(16),
                     "product_name": "ALPHA91", "quantity": 3,
                     "customer": "SYNTHETIC-F3", "due_date": "2026-10-05"}
        self.idempotency_key = secrets.token_hex(24)
        self.process = None
        self.log = None
        self.client = None

    def start(self) -> None:
        """启动原Python独立进程，从600就绪文件读取动态端口。"""
        ready = self.runtime / "ready.private.json"
        if ready.exists():
            ready.unlink()
        self.log = (self.runtime / "server.private.log").open("ab")
        os.chmod(self.runtime / "server.private.log", 0o600)
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("final_f3_simulated_provider.py")),
             "--runtime", str(self.runtime)], env=self.environment(),
            stdout=self.log, stderr=self.log,
        )
        deadline = time.monotonic() + 5
        while not ready.is_file() and self.process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.is_file(), "OWNED_SIMULATION_DID_NOT_START"
        marker = json.loads(ready.read_text())
        assert marker["pid"] == self.process.pid
        assert marker["provider_contract"] == "f3-project-provider-v1"
        assert marker["project_provider_contract_pass"] is True
        assert marker["real_vendor_qualified"] is False
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{marker['port']}",
                                   timeout=2, trust_env=False)

    def environment(self) -> dict[str, str]:
        """只传本例生成的模拟授权，禁止继承真实模型/数据库/供应商环境。"""
        return {"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1", "LANG": "C.UTF-8",
                "F3_SIMULATION_KEY": self.key, "F3_SIMULATION_CONTROL_KEY": self.control_key,
                "F3_SIMULATION_NAMESPACE": self.namespace}

    def stop(self) -> None:
        """仅回收本例Popen身份及客户端，不按端口停止其他服务。"""
        if self.client:
            self.client.close()
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
            assert self.process.poll() is not None
        if self.log:
            self.log.close()

    def submit(self, *, body=None, key=None, authorization=None):
        """经真实HTTP提交；只有调用者显式选择时才发送错误授权。"""
        return self.client.post("/simulation/tasks", json=self.body if body is None else body,
                                headers={"X-Simulation-Key": self.key if authorization is None
                                         else authorization,
                                         "Idempotency-Key": self.idempotency_key if key is None
                                         else key})

    def readback(self):
        """独立业务接口读对象存在性及实际效果次数，不借任务状态判成功。"""
        return self.client.get("/simulation/objects/" + self.body["object_id"],
                               headers={"X-Simulation-Key": self.key})

    def control(self, path: str, *, body=None):
        """测试控制只推进模拟远端，不写原Operation、Inbox或Signal。"""
        return self.client.post("/simulation/control/" + path,
                                headers={"X-Simulation-Control-Key": self.control_key},
                                json={} if body is None else body)


@pytest.fixture
def remote(tmp_path: Path):
    """串行启动且总是清理自有模拟进程；异常也不能遗留服务。"""
    service = RemoteSimulation(tmp_path)
    try:
        service.start()
        yield service
    finally:
        service.stop()


def task_id(response) -> str:
    """先核实际HTTP202与accepted，再取随机远端任务身份。"""
    assert response.status_code == 202
    packet = response.json()
    assert packet["status"] == "accepted"
    assert packet["provider_contract"] == "f3-project-provider-v1"
    assert packet["project_provider_contract_pass"] is True
    assert packet["real_vendor_qualified"] is False
    return packet["taskId"]


def test_accepted_is_not_effect_and_poll_has_no_submission_auth(remote) -> None:
    """受理后业务对象仍不存在，无提交授权的原GET能够查询pending状态。"""
    identity = task_id(remote.submit())
    assert remote.readback().status_code == 404
    status = remote.client.get("/simulation/tasks/" + identity)
    assert status.status_code == 200 and status.json()["status"] == "accepted"
    assert status.json()["result"] is None


def test_normal_completion_is_independently_read_back_once(remote) -> None:
    """重复完成和重复提交保留一个真实模拟业务对象，独立读回计数为1。"""
    identity = task_id(remote.submit())
    assert remote.control(identity + "/complete").status_code == 200
    assert remote.control(identity + "/complete").status_code == 200
    duplicate = remote.submit()
    assert duplicate.status_code == 202 and duplicate.json()["taskId"] == identity
    effect = remote.readback().json()
    assert effect["effect_count"] == 1 and effect["request_count"] == 2
    assert effect["business"] == remote.body and effect["taskId"] == identity
    assert remote.client.get("/simulation/tasks/" + identity).json()["status"] == "completed"


def test_changed_arguments_under_old_idempotency_key_are_rejected(remote) -> None:
    """原幂等键不可接收新参数，冲突不篡改原任务或产生业务效果。"""
    identity = task_id(remote.submit())
    changed = {**remote.body, "quantity": 4}
    assert remote.submit(body=changed).status_code == 409
    assert remote.readback().status_code == 404
    assert remote.control(identity + "/complete").status_code == 200
    assert remote.readback().json()["business"]["quantity"] == 3


@pytest.mark.parametrize("condition", ["wrong_auth", "wrong_namespace", "missing_key",
                                       "boolean_quantity", "unknown_field", "safe_decline",
                                       "array_product", "object_product"])
def test_rejected_submission_has_zero_effect(remote, condition: str) -> None:
    """越权和明确模拟业务拒绝均零效果，错误类型保持独立可核。"""
    body, key, auth = dict(remote.body), None, None
    expected = 422
    if condition == "wrong_auth":
        auth, expected = "WRONG-SYNTHETIC-AUTH", 401
    elif condition == "wrong_namespace":
        body["namespace"], expected = "other-synthetic", 403
    elif condition == "missing_key":
        key, expected = "", 400
    elif condition == "boolean_quantity":
        body["quantity"] = True
    elif condition == "unknown_field":
        body["unexpected"] = "SYNTHETIC"
    elif condition in {"array_product", "object_product"}:
        body["product_name"] = [] if condition == "array_product" else {}
    else:
        body["product_name"] = "SIMULATED_DECLINE"
    response = remote.submit(body=body, key=key, authorization=auth)
    assert response.status_code == expected and remote.readback().status_code == 404
    task_id(remote.submit())


def test_cancelled_remote_task_cannot_later_create_effect(remote) -> None:
    """模拟远端撤销后拒绝迟到完成；本地取消与远端撤销不是同一状态。"""
    identity = task_id(remote.submit())
    assert remote.control(identity + "/cancel").status_code == 200
    assert remote.control(identity + "/complete").status_code == 409
    assert remote.readback().status_code == 404
    assert remote.client.get("/simulation/tasks/" + identity).json()["status"] == "cancelled"


def test_cleanup_is_read_back_and_old_key_cannot_resurrect_object(remote) -> None:
    """独立清理确认对象消失，历史键/迟到完成不能重新创建已清理效果。"""
    identity = task_id(remote.submit())
    assert remote.control(identity + "/complete").status_code == 200
    cleaned = remote.client.delete("/simulation/objects/" + remote.body["object_id"],
                                   headers={"X-Simulation-Key": remote.key})
    assert cleaned.status_code == 200 and remote.readback().status_code == 404
    assert remote.submit().json()["taskId"] == identity
    assert remote.control(identity + "/complete").status_code == 200
    assert remote.readback().status_code == 404


def test_real_new_pid_preserves_task_effect_and_idempotency(remote) -> None:
    """停止并新建真正服务进程，原受理任务和唯一效果通过持久远端库恢复。"""
    identity = task_id(remote.submit())
    old_pid = remote.process.pid
    remote.stop()
    remote.start()
    assert remote.process.pid != old_pid
    assert remote.client.get("/simulation/tasks/" + identity).json()["status"] == "accepted"
    assert remote.control(identity + "/complete").status_code == 200
    remote.stop()
    remote.start()
    assert remote.submit().json()["taskId"] == identity
    assert remote.readback().json()["effect_count"] == 1


def test_lost_accept_response_is_reconciled_before_any_reissue(remote) -> None:
    """真实HTTP连接丢回执后先按原幂等摘要查远端，只完成原任务不盲重发。"""
    assert remote.control("fault", body={"mode": "drop_after_accept"}).status_code == 200
    with pytest.raises(httpx.RemoteProtocolError):
        remote.submit()
    key_hash = hashlib.sha256(remote.idempotency_key.encode()).hexdigest()
    state = remote.client.get("/simulation/reconcile/" + key_hash,
                              headers={"X-Simulation-Key": remote.key}).json()
    assert state["request_count"] == 1 and state["effect_count"] == 0
    assert remote.control(state["taskId"] + "/complete").status_code == 200
    assert remote.readback().json()["effect_count"] == 1
    assert remote.readback().json()["request_count"] == 1


def test_original_http_submission_and_poll_use_unmodified_contract(remote, monkeypatch) -> None:
    """原ToolExecutor提交和原poll经真实HTTP读取同一服务，不替换SDK或HTTP响应。"""
    from sqlmodel import Session, create_engine
    from app.config import get_settings
    from app.db.models import SopOperation, Tool
    from app.dynamic_tasks.capability_catalog import ToolReliabilityContract
    from app.sop_runtime.external_tasks import _poll_provider_task
    from app.tools.tool_executor import ToolExecutor

    monkeypatch.setenv("TOOL_OUTBOUND_ALLOWED_HOSTS", "127.0.0.1")
    get_settings.cache_clear()
    contract = ToolReliabilityContract.model_validate({
        "risk_class": "external_write", "side_effect": "external",
        "confirmation_policy": "once", "timeout_policy": "unknown",
        "dynamic_task_enabled": True, "idempotency": {"mode": "request_key",
                                                       "remote_scope": remote.namespace},
        "execution_mode": "detached", "async_strategy": "provider_task",
        "status_url": str(remote.client.base_url).rstrip("/") + "/simulation/tasks/{taskId}",
        "status_mapping": {"accepted": "working"},
    })
    tool = Tool(tenant_id="SIMULATED-TENANT", name="simulation.submit", tool_type="http",
                method="POST", url=str(remote.client.base_url).rstrip("/") + "/simulation/tasks",
                headers_json={"X-Simulation-Key": remote.key})
    try:
        with Session(create_engine("sqlite://")) as db:
            result = ToolExecutor(db)._execute_provider_task(
                tool, remote.body, contract, remote_idempotency_key=remote.idempotency_key,
                is_cancelled=None)
        assert result.disposition == "accepted" and result.success is False, (
            result.error.code if result.error else "SIMULATION_SUBMISSION_CONTRACT_MISMATCH")
        identity = result.data["provider_task_id"]
        operation = SopOperation(capability_snapshot_json={"contract": contract.model_dump()})
        frozen_contract = operation.capability_snapshot_json["contract"]
        before = _poll_provider_task(frozen_contract, task_id=identity, status_url=contract.status_url)
        assert before["status"] == "working" and remote.readback().status_code == 404
        assert remote.control(identity + "/complete").status_code == 200
        after = _poll_provider_task(frozen_contract, task_id=identity, status_url=contract.status_url)
        assert after["status"] == "completed" and after["result"] == remote.readback().json()["business"]
        assert remote.readback().json()["request_count"] == 1
    finally:
        get_settings.cache_clear()


def test_simulation_does_not_supply_models_or_claim_vendor_identity(remote) -> None:
    """模拟进程没有模型接口，显式身份永远是模拟而非真实供应商。"""
    response = remote.client.get("/simulation/health")
    assert response.status_code == 200
    assert response.json()["provider_kind"] == "simulated_async_service"
    assert response.json()["provider_contract"] == "f3-project-provider-v1"
    assert response.json()["project_provider_contract_pass"] is True
    assert response.json()["real_vendor_qualified"] is False
    assert response.json()["model_api_installed"] is False
    assert remote.client.post("/v1/chat/completions", json={}).status_code == 404


def test_submission_key_cannot_complete_cancel_or_inject_fault(remote) -> None:
    """普通工具的提交key无权访问模拟控制，不能自行伪造完成或撤销效果。"""
    identity = task_id(remote.submit())
    for path in (identity + "/complete", identity + "/cancel", "fault"):
        response = remote.client.post("/simulation/control/" + path, json={},
                                      headers={"X-Simulation-Control-Key": remote.key})
        assert response.status_code == 401
    assert remote.readback().status_code == 404
    assert remote.client.get("/simulation/tasks/" + identity).json()["status"] == "accepted"


def test_pending_cleanup_cannot_allow_late_effect_resurrection(remote) -> None:
    """未完成任务先清理须拒绝，避免清理后排队任务迟到产生新效果。"""
    identity = task_id(remote.submit())
    response = remote.client.delete("/simulation/objects/" + remote.body["object_id"],
                                    headers={"X-Simulation-Key": remote.key})
    assert response.status_code == 409
    assert remote.control(identity + "/complete").status_code == 200
    assert remote.readback().json()["effect_count"] == 1


@pytest.mark.parametrize("artifact,link_kind", [
    ("ready.private.json", "symlink"), (".f3-simulation-owned.json", "symlink"),
    ("provider.sqlite3", "symlink"), ("ready.private.json", "hardlink"),
    (".f3-simulation-owned.json", "hardlink"), ("provider.sqlite3", "hardlink"),
])
def test_owned_artifact_links_are_refused_without_writing_target(
    tmp_path: Path, artifact: str, link_kind: str,
) -> None:
    """自有服务入口拒绝ready/marker/db链接替身，外部原文件字节不能被改写。"""
    service = RemoteSimulation(tmp_path)
    target = tmp_path / "outside-synthetic-original.txt"
    original = b"ORIGINAL_SYNTHETIC_ARTIFACT\n"
    target.write_bytes(original)
    target.chmod(0o600)
    if link_kind == "symlink":
        (service.runtime / artifact).symlink_to(target)
    else:
        os.link(target, service.runtime / artifact)
    with (service.runtime / "refusal.private.log").open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("final_f3_simulated_provider.py")),
             "--runtime", str(service.runtime)], env=service.environment(),
            stdout=log, stderr=log)
        rejected = False
        try:
            try:
                rejected = process.wait(timeout=0.5) != 0
            except subprocess.TimeoutExpired:
                pass
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=3)
    assert rejected, "UNSAFE_OWNED_ARTIFACT_WAS_ACCEPTED"
    assert target.read_bytes() == original
