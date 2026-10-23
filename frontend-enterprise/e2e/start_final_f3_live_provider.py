"""
@Time       : 2026/10/05
@Author     : zhanglp8181
@File       : start_final_f3_live_provider.py
@CallChain  : 受保护F3子树 → 原独立模拟HTTP → 每请求安全旁证/读回
@Description: 把基础模拟服务接入轨道A合同旁证和真实HTTP callback；不改受理/状态/效果合同，来源始终为模拟。
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import final_f3_simulated_provider as original

MAX_PROVIDER_OBSERVATION_ROWS = 128
MAX_PROVIDER_OBSERVATION_BYTES = 256 * 1024
MAX_PROVIDER_OBSERVATION_BODY_BYTES = 256 * 1024
MAX_PROVIDER_OBSERVATION_ROW_BYTES = 4096
MAX_CALLBACK_BINDINGS = 128
MAX_CALLBACK_BINDING_BYTES = 64 * 1024
_CALLBACK_BINDING_FIELDS = frozenset({"task_id", "token", "submission_id", "mac"})
_PROVIDER_ROW_FIELDS = frozenset({
    "method", "path_sha256", "is_submit", "pid", "started_at_utc", "status",
    "response_intentionally_dropped",
})


def _callback_binding_mac(task_id: str, token: str, submission_id: str | None,
                          control_key: str) -> str:
    """用测试控制密钥完整性保护回调绑定，旁证文件不把绑定当业务事实。"""
    packet = json.dumps([task_id, token, submission_id], ensure_ascii=False,
                        separators=(",", ":")).encode("utf-8")
    return hmac.new(control_key.encode("utf-8"), packet, hashlib.sha256).hexdigest()


def _read_callback_bindings(path: Path, control_key: str) -> dict[str, dict[str, str | None]]:
    """真重启时读取预算内的私有回调凭据绑定，并逐项验证控制密钥摘要。"""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or info.st_size > MAX_CALLBACK_BINDING_BYTES):
        raise ValueError("F3_CALLBACK_BINDING_ARCHIVE_INVALID")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        current = os.fstat(descriptor)
        if (current.st_ino != info.st_ino or current.st_size != info.st_size
                or current.st_size > MAX_CALLBACK_BINDING_BYTES):
            raise ValueError("F3_CALLBACK_BINDING_ARCHIVE_INVALID")
        data = bytearray()
        while chunk := os.read(descriptor, MAX_CALLBACK_BINDING_BYTES + 1 - len(data)):
            data.extend(chunk)
            if len(data) > MAX_CALLBACK_BINDING_BYTES:
                raise ValueError("F3_CALLBACK_BINDING_ARCHIVE_INVALID")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(data)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("F3_CALLBACK_BINDING_ARCHIVE_INVALID") from exc
    if (not isinstance(value, dict)
            or value.keys() != {"schema_version", "owner_uid", "bindings"}
            or value["schema_version"] != "f3-callback-bindings-v1"
            or value["owner_uid"] != os.getuid()
            or not isinstance(value["bindings"], list)
            or len(value["bindings"]) > MAX_CALLBACK_BINDINGS):
        raise ValueError("F3_CALLBACK_BINDING_ARCHIVE_INVALID")
    bindings: dict[str, dict[str, str | None]] = {}
    for item in value["bindings"]:
        if (not isinstance(item, dict) or item.keys() != _CALLBACK_BINDING_FIELDS
                or not isinstance(item["task_id"], str)
                or not re.fullmatch(r"f3-task-[0-9a-f]{32}", item["task_id"])
                or not isinstance(item["token"], str) or not 1 <= len(item["token"]) <= 1024
                or item["submission_id"] is not None
                and (not isinstance(item["submission_id"], str)
                     or len(item["submission_id"]) > 512)
                or not isinstance(item["mac"], str)
                or not hmac.compare_digest(
                    item["mac"],
                    _callback_binding_mac(item["task_id"], item["token"],
                                          item["submission_id"], control_key),
                )):
            raise ValueError("F3_CALLBACK_BINDING_ARCHIVE_INVALID")
        if item["task_id"] in bindings:
            raise ValueError("F3_CALLBACK_BINDING_ARCHIVE_INVALID")
        bindings[item["task_id"]] = {
            "token": item["token"], "submission_id": item["submission_id"],
        }
    return bindings


def _write_callback_bindings(path: Path, bindings: dict[str, dict[str, str | None]],
                             control_key: str) -> None:
    """原子写入600权限回调绑定，失败时不扩展任何业务旁证或日志。"""
    if len(bindings) > MAX_CALLBACK_BINDINGS:
        raise ValueError("F3_CALLBACK_BINDING_LIMIT")
    original._private_file(path)
    rows = []
    for task_id, binding in sorted(bindings.items()):
        token, submission_id = binding.get("token"), binding.get("submission_id")
        if (not isinstance(token, str) or not 1 <= len(token) <= 1024
                or submission_id is not None
                and (not isinstance(submission_id, str) or len(submission_id) > 512)):
            raise ValueError("F3_CALLBACK_BINDING_INVALID")
        rows.append({
            "task_id": task_id, "token": token, "submission_id": submission_id,
            "mac": _callback_binding_mac(task_id, token, submission_id, control_key),
        })
    packet = json.dumps({"schema_version": "f3-callback-bindings-v1",
                         "owner_uid": os.getuid(), "bindings": rows},
                        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(packet) > MAX_CALLBACK_BINDING_BYTES:
        raise ValueError("F3_CALLBACK_BINDING_ARCHIVE_LIMIT")
    temporary = path.with_name(path.name + ".writing")
    original._private_file(temporary)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        remaining = memoryview(packet)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("F3_CALLBACK_BINDING_WRITE_FAILED")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def _read_provider_observations(path: Path) -> list[dict]:
    """真重启时只恢复预算内的安全固定字段，不接受过大或不完整归档。"""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return []
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or info.st_size > MAX_PROVIDER_OBSERVATION_BYTES):
        raise ValueError("F3_PROVIDER_OBSERVATION_ARCHIVE_INVALID")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        current = os.fstat(descriptor)
        if (current.st_ino != info.st_ino or current.st_size != info.st_size
                or current.st_size > MAX_PROVIDER_OBSERVATION_BYTES):
            raise ValueError("F3_PROVIDER_OBSERVATION_ARCHIVE_INVALID")
        data = bytearray()
        while chunk := os.read(descriptor, MAX_PROVIDER_OBSERVATION_BYTES + 1 - len(data)):
            data.extend(chunk)
            if len(data) > MAX_PROVIDER_OBSERVATION_BYTES:
                raise ValueError("F3_PROVIDER_OBSERVATION_ARCHIVE_INVALID")
    finally:
        os.close(descriptor)
    if data and not data.endswith(b"\n"):
        raise ValueError("F3_PROVIDER_OBSERVATION_ARCHIVE_INVALID")
    lines = data.splitlines()
    if len(lines) > MAX_PROVIDER_OBSERVATION_ROWS:
        raise ValueError("F3_PROVIDER_OBSERVATION_ARCHIVE_INVALID")
    rows = []
    for line in lines:
        value = json.loads(line)
        if (not isinstance(value, dict) or value.keys() != _PROVIDER_ROW_FIELDS
                or value["method"] not in {"GET", "POST"}
                or not isinstance(value["path_sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", value["path_sha256"])
                or type(value["is_submit"]) is not bool
                or type(value["pid"]) is not int or value["pid"] <= 0
                or not isinstance(value["started_at_utc"], str)
                or len(value["started_at_utc"]) > 64
                or value["status"] is not None and (type(value["status"]) is not int
                    or not 100 <= value["status"] <= 599)
                or type(value["response_intentionally_dropped"]) is not bool):
            raise ValueError("F3_PROVIDER_OBSERVATION_ARCHIVE_INVALID")
        rows.append(value)
    return rows


def _append_provider_observation(path: Path, row: dict, existing_rows: int) -> None:
    """在严格单文件预算内安全追加一行；已满或损坏归档拒绝扩写。"""
    if existing_rows >= MAX_PROVIDER_OBSERVATION_ROWS:
        raise ValueError("F3_PROVIDER_OBSERVATION_ROW_LIMIT")
    packet = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(packet) > MAX_PROVIDER_OBSERVATION_ROW_BYTES:
        raise ValueError("F3_PROVIDER_OBSERVATION_ROW_BYTE_LIMIT")
    descriptor = os.open(
        path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600,
    )
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError("F3_PROVIDER_OBSERVATION_ARCHIVE_INVALID")
        if info.st_size + len(packet) > MAX_PROVIDER_OBSERVATION_BYTES:
            raise ValueError("F3_PROVIDER_OBSERVATION_BYTE_LIMIT")
        data = memoryview(packet)
        while data:
            try:
                written = os.write(descriptor, data)
            except InterruptedError:
                continue
            if written <= 0:
                raise OSError("F3_PROVIDER_OBSERVATION_WRITE_FAILED")
            data = data[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install_http_observation(runtime: Path) -> None:
    """围绕原路由透传请求和异常，独立计数实际提交，读回接口不改变任务或计入提交。"""
    route = original.SimulationHandler._route
    try:
        observations = _read_provider_observations(runtime / "http-observations.safe.jsonl")
        observation_errors = 0
    except (OSError, ValueError, TypeError):
        observations = []
        observation_errors = 1
    control_key = os.environ.get("F3_SIMULATION_CONTROL_KEY", "")
    callback_binding_path = runtime / "callback-bindings.private.json"
    callback_bindings = _read_callback_bindings(callback_binding_path, control_key)
    log_path = runtime / "http-observations.safe.jsonl"

    def observed_route(handler) -> tuple[int, dict]:
        """仅输出方法/路径摘要/状态/合成来源，授权和业务正文均由原路由处理且不记录。"""
        nonlocal observation_errors
        if handler.command == "GET" and handler.path == "/simulation/observations":
            handler._authorize()
            value = {**original.provider_contract_metadata(), "rows": list(observations),
                     "submit_http_count": sum(row["is_submit"] for row in observations),
                     "observation_healthy": observation_errors == 0,
                     "observation_error_count": observation_errors}
            encoded_size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            if encoded_size > MAX_PROVIDER_OBSERVATION_BODY_BYTES:
                observation_errors += 1
                return 503, {**original.provider_contract_metadata(), "rows": [],
                             "submit_http_count": 0, "observation_healthy": False,
                             "observation_error_count": observation_errors}
            return 200, value
        row = {"method": handler.command, "path_sha256": hashlib.sha256(handler.path.encode()).hexdigest(),
               "is_submit": handler.command == "POST" and handler.path == "/simulation/tasks",
               "pid": os.getpid(), "started_at_utc": datetime.now(UTC).isoformat(), "status": None,
               "response_intentionally_dropped": False}
        try:
            if len(observations) >= MAX_PROVIDER_OBSERVATION_ROWS:
                raise original.SimulationRejected(429, "SIMULATED_OBSERVATION_BUDGET_EXHAUSTED")
            # 只有测试控制面可以触发本轮 provider callback；callback 本身仍通过真实 HTTP
            # 请求进入应用原有接口，不能由测试代码直接写入 Inbox、Signal 或 Operation。
            callback_match = re.fullmatch(r"/simulation/control/(f3-task-[0-9a-f]{32})/callback", handler.path)
            if handler.command == "POST" and callback_match:
                handler._authorize(control=True)
                body = handler._body()
                if not isinstance(body, dict) or set(body) != {"operation_id", "provider_event_id", "observation_revision"} \
                        or not all(isinstance(body[field], str) and re.fullmatch(r"[A-Za-z0-9_:.-]{1,512}", body[field])
                                   for field in ("operation_id", "provider_event_id")) \
                        or type(body["observation_revision"]) is not int or body["observation_revision"] < 1:
                    raise original.SimulationRejected(422, "SIMULATED_CALLBACK_FIELDS_INVALID")
                task_id = callback_match[1]
                task = handler.server.store.status(task_id)
                if task["status"] != "completed":
                    raise original.SimulationRejected(409, "SIMULATED_CALLBACK_EFFECT_REQUIRED")
                binding = callback_bindings.get(task_id)
                if not binding or not binding["token"]:
                    raise original.SimulationRejected(409, "SIMULATED_CALLBACK_ORIGINAL_TOKEN_REQUIRED")
                effect = handler.server.store.readback(task["result"]["object_id"])
                origin = os.environ.get("F3_LIVE_APP_ORIGIN", "")
                target = urlsplit(origin)
                if target.scheme != "http" or target.hostname != "127.0.0.1" or not target.port \
                        or target.username or target.password or target.query or target.fragment \
                        or target.path not in {"", "/"}:
                    raise original.SimulationRejected(422, "SIMULATED_CALLBACK_OWNED_ORIGIN_REQUIRED")
                packet = {"provider_task_id": task_id, "provider_event_id": body["provider_event_id"],
                          "provider_status": "completed", "observation_revision": body["observation_revision"],
                          "external_submission_id": binding["submission_id"],
                          "payload": {"data": {**effect["business"], "receipt_id": effect["receipt_id"]}}}
                request = Request(origin.rstrip("/") + "/api/external-tasks/" + body["operation_id"] + "/callback",
                                  data=json.dumps(packet).encode(), method="POST", headers={
                                      "Content-Type": "application/json",
                                      "X-Gongge-Xuban-External-Task-Token": binding["token"]})
                try:
                    with urlopen(request, timeout=10) as response:
                        callback_status, callback_body = response.status, response.read()
                except HTTPError as error:
                    callback_status, callback_body = error.code, error.read()
                status, value = 200, {**original.provider_contract_metadata(),
                                      "callback_http_status": callback_status,
                                      "callback_response_sha256": hashlib.sha256(callback_body).hexdigest(),
                                      "operation_id": body["operation_id"]}
            else:
                status, value = route(handler)
            row["status"] = status
            row["response_intentionally_dropped"] = bool(
                row["is_submit"] and status == 202 and handler.server.drop_next_accept
            )
            if row["is_submit"] and status == 202 and observation_errors == 0 \
                    and len(observations) < MAX_PROVIDER_OBSERVATION_ROWS:
                headers = getattr(handler, "headers", {})
                token = headers.get("X-Gongge-Xuban-External-Task-Token")
                submission_id = headers.get("X-Gongge-Xuban-External-Submission-Id")
                if token:
                    binding = {"token": token, "submission_id": submission_id}
                    previous = callback_bindings.get(value["taskId"])
                    if previous is not None and previous != binding:
                        raise original.SimulationRejected(
                            409, "SIMULATED_CALLBACK_TOKEN_CONFLICT",
                        )
                    callback_bindings[value["taskId"]] = binding
                    try:
                        _write_callback_bindings(callback_binding_path, callback_bindings, control_key)
                    except (OSError, ValueError) as exc:
                        raise original.SimulationRejected(
                            503, "SIMULATED_CALLBACK_BINDING_PERSISTENCE_FAILED",
                        ) from exc
            if value.get("status") == "completed" and isinstance(value.get("result"), dict):
                try:
                    effect = handler.server.store.readback(value["result"]["object_id"])
                except original.SimulationRejected as error:
                    if error.status != 404:
                        raise
                else:
                    value = {**value, "result": {**value["result"], "receipt_id": effect["receipt_id"]}}
            return status, value
        except original.SimulationRejected as exc:
            row["status"] = exc.status
            raise
        finally:
            record_in_memory = len(observations) < MAX_PROVIDER_OBSERVATION_ROWS
            if record_in_memory:
                observations.append(row)
            try:
                if observation_errors or not record_in_memory:
                    raise ValueError("F3_PROVIDER_OBSERVATION_EVIDENCE_ALREADY_INVALID")
                _append_provider_observation(log_path, row, len(observations) - 1)
            except Exception:
                observation_errors += 1

    original.SimulationHandler._route = observed_route


def main() -> int:
    """保持原CLI与HTTP监听；测试侧旁证没有模型接口、产品Runtime或供应商资格。"""
    if len(sys.argv) != 3 or sys.argv[1] != "--runtime":
        raise RuntimeError("F3_LIVE_PROVIDER_OWNED_CLI_REQUIRED")
    install_http_observation(Path(sys.argv[2]))
    return original.main()


if __name__ == "__main__":
    raise SystemExit(main())
