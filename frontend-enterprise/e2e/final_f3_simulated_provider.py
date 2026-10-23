"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : final_f3_simulated_provider.py
@CallChain  : 自有测试进程 → 独立HTTP模拟服务 → 持久任务/业务效果/清理读回
@Description: 作为项目provider接口合同的隔离规范实现，覆盖提交、查询、回调、幂等、独立读回和清理；不操作应用数据库。
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
from uuid import uuid4


PROJECT_PROVIDER_CONTRACT = "f3-project-provider-v1"
PROJECT_PROVIDER_CONTRACT_PASS = True


def provider_contract_metadata() -> dict[str, object]:
    """返回所有模拟接口共享的项目provider合同身份，不冒称第三方供应商。"""
    return {
        "provider_kind": "simulated_async_service",
        "provider_contract": PROJECT_PROVIDER_CONTRACT,
        "project_provider_contract_pass": PROJECT_PROVIDER_CONTRACT_PASS,
        "real_vendor_qualified": False,
    }


def _digest(value: str) -> str:
    """对合成参数或幂等键作SHA256，不把授权原值存入任务库。"""
    return hashlib.sha256(value.encode()).hexdigest()


def _private_file(path: Path) -> None:
    """已有文件须为当前UID的600单链接普通文件，拒绝链接替身。"""
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise ValueError("F3_SIMULATION_PRIVATE_REGULAR_FILE_REQUIRED")


def _write_private(path: Path, value: object) -> None:
    """只覆盖自有私有普通文件，打开后再次校验原inode和链接数。"""
    _private_file(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1
                or info.st_ino != path.lstat().st_ino):
            raise ValueError("F3_SIMULATION_PRIVATE_REGULAR_FILE_REQUIRED")
        packet = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
        os.ftruncate(descriptor, 0)
        while packet:
            written = os.write(descriptor, packet)
            if written <= 0:
                raise ValueError("F3_SIMULATION_PRIVATE_WRITE_FAILED")
            packet = packet[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class SimulationRejected(ValueError):
    """只携带稳定HTTP状态与模拟错误类别，不拼接请求或秘密。"""

    def __init__(self, status: int, code: str):
        """保持拒绝的真实状态与稳定错误码。"""
        self.status, self.code = status, code
        super().__init__(code)


class SimulationStore:
    """模拟供应商独立的SQLite任务和业务账本，不读写应用数据库。"""

    def __init__(self, runtime: Path, namespace: str):
        """核自有标记和文件边界后初始化合成数据库，真重启保留原账本。"""
        self.namespace = namespace
        marker = runtime / ".f3-simulation-owned.json"
        database = runtime / "provider.sqlite3"
        for name in (marker, database, runtime / "ready.private.json"):
            _private_file(name)
        ownership = {"schema_version": "f3-simulated-provider-v1", "owner_uid": os.getuid(),
                     "namespace_sha256": _digest(namespace),
                     "provider_kind": "simulated_async_service"}
        if marker.exists():
            if json.loads(marker.read_text()) != ownership:
                raise ValueError("F3_SIMULATION_OWNERSHIP_MISMATCH")
        elif database.exists():
            raise ValueError("F3_SIMULATION_UNOWNED_DATABASE_REFUSED")
        else:
            _write_private(marker, ownership)
        self.db = sqlite3.connect(database)
        database.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, key_hash TEXT UNIQUE NOT NULL,
                object_id TEXT UNIQUE NOT NULL, body_json TEXT NOT NULL,
                request_hash TEXT NOT NULL, status TEXT NOT NULL,
                submit_calls INTEGER NOT NULL DEFAULT 1,
                effect_count INTEGER NOT NULL DEFAULT 0,
                cleaned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS effects (
                object_id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL,
                business_json TEXT NOT NULL, receipt_id TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id)
            );
        """)

    def _task(self, identity: str) -> sqlite3.Row:
        """按随机任务身份读远端事实，未知任务明确404。"""
        row = self.db.execute("SELECT * FROM tasks WHERE task_id=?", (identity,)).fetchone()
        if row is None:
            raise SimulationRejected(404, "SIMULATED_TASK_NOT_FOUND")
        return row

    def _validate_body(self, body: object) -> dict:
        """只接本轮合成命名空间和冻结业务字段，不接受用户正文或额外属性。"""
        fields = {"namespace", "object_id", "product_name", "quantity", "customer", "due_date"}
        if not isinstance(body, dict) or set(body) != fields:
            raise SimulationRejected(422, "SIMULATED_BODY_SCHEMA_INVALID")
        if body["namespace"] != self.namespace:
            raise SimulationRejected(403, "SIMULATED_NAMESPACE_FORBIDDEN")
        valid = (isinstance(body["object_id"], str)
                 and re.fullmatch(r"f3-obj-[0-9a-f]{32}", body["object_id"])
                 and isinstance(body["product_name"], str)
                 and body["product_name"] in {"ALPHA91", "SIMULATED_DECLINE"}
                 and type(body["quantity"]) is int and 1 <= body["quantity"] <= 10
                 and body["customer"] == "SYNTHETIC-F3"
                 and isinstance(body["due_date"], str))
        try:
            valid = valid and date.fromisoformat(body["due_date"]).isoformat() == body["due_date"]
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise SimulationRejected(422, "SIMULATED_BODY_SCHEMA_INVALID")
        if body["product_name"] == "SIMULATED_DECLINE":
            raise SimulationRejected(422, "SIMULATED_BUSINESS_DECLINED")
        return body

    def submit(self, body: object, key: str) -> dict:
        """同键同参数复用任务，同键异参数409；受理时不写业务效果。"""
        body = self._validate_body(body)
        if not key or len(key) > 256:
            raise SimulationRejected(400, "SIMULATED_IDEMPOTENCY_KEY_REQUIRED")
        encoded = json.dumps(body, sort_keys=True, ensure_ascii=False)
        key_hash, request_hash = _digest(key), _digest(encoded)
        with self.db:
            old = self.db.execute("SELECT * FROM tasks WHERE key_hash=?", (key_hash,)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise SimulationRejected(409, "SIMULATED_IDEMPOTENCY_CONFLICT")
                self.db.execute("UPDATE tasks SET submit_calls=submit_calls+1 WHERE task_id=?",
                                (old["task_id"],))
                return self.status(old["task_id"])
            if self.db.execute("SELECT 1 FROM tasks WHERE object_id=?", (body["object_id"],)).fetchone():
                raise SimulationRejected(409, "SIMULATED_OBJECT_ID_ALREADY_USED")
            identity, now = "f3-task-" + uuid4().hex, datetime.now(UTC).isoformat()
            self.db.execute(
                "INSERT INTO tasks (task_id,key_hash,object_id,body_json,request_hash,status,"
                "created_at,updated_at) VALUES (?,?,?,?,?,'accepted',?,?)",
                (identity, key_hash, body["object_id"], encoded, request_hash, now, now))
        return self.status(identity)

    def status(self, identity: str) -> dict:
        """任务状态只来自持久远端账本，accepted不会包含完成结果。"""
        row = self._task(identity)
        return {"taskId": identity, "status": row["status"],
                "result": json.loads(row["body_json"]) if row["status"] == "completed" else None,
                **provider_contract_metadata()}

    def control(self, identity: str, action: str) -> dict:
        """仅远端控制完成/撤销，完成原子写一次效果，终态不回退或复活。"""
        with self.db:
            row = self._task(identity)
            if action == "complete":
                if row["status"] == "cancelled":
                    raise SimulationRejected(409, "SIMULATED_TASK_ALREADY_CANCELLED")
                if row["status"] != "completed":
                    self.db.execute("INSERT INTO effects VALUES (?,?,?,?)",
                                    (row["object_id"], identity, row["body_json"],
                                     "f3-receipt-" + uuid4().hex))
                    self.db.execute("UPDATE tasks SET status='completed',effect_count=1,"
                                    "updated_at=? WHERE task_id=?",
                                    (datetime.now(UTC).isoformat(), identity))
            elif action == "cancel":
                if row["status"] == "completed":
                    raise SimulationRejected(409, "SIMULATED_TASK_ALREADY_COMPLETED")
                self.db.execute("UPDATE tasks SET status='cancelled',updated_at=? WHERE task_id=?",
                                (datetime.now(UTC).isoformat(), identity))
            else:
                raise SimulationRejected(404, "SIMULATED_CONTROL_NOT_FOUND")
        return self.status(identity)

    def readback(self, object_id: str) -> dict:
        """从独立effects表读取已发生业务对象，而非从accepted状态猜效果。"""
        effect = self.db.execute("SELECT * FROM effects WHERE object_id=?", (object_id,)).fetchone()
        if effect is None:
            raise SimulationRejected(404, "SIMULATED_OBJECT_ABSENT")
        row = self._task(effect["task_id"])
        return {"taskId": row["task_id"], "business": json.loads(effect["business_json"]),
                "receipt_id": effect["receipt_id"], "effect_count": row["effect_count"],
                "request_count": row["submit_calls"], **provider_contract_metadata()}

    def reconcile(self, key_hash: str) -> dict:
        """回执失联时独立按幂等摘要读原任务及效果，不执行任何重新提交。"""
        row = self.db.execute("SELECT * FROM tasks WHERE key_hash=?", (key_hash,)).fetchone()
        if row is None:
            raise SimulationRejected(404, "SIMULATED_RECONCILIATION_ABSENT")
        return {**self.status(row["task_id"]), "request_count": row["submit_calls"],
                "effect_count": row["effect_count"], "object_cleaned": bool(row["cleaned"])}

    def cleanup(self, object_id: str) -> dict:
        """清理已终结的合成业务对象，保留任务幂等墓碑防迟到完成复活。"""
        with self.db:
            row = self.db.execute("SELECT * FROM tasks WHERE object_id=?", (object_id,)).fetchone()
            if row is None:
                raise SimulationRejected(404, "SIMULATED_OBJECT_ABSENT")
            if row["status"] == "accepted":
                raise SimulationRejected(409, "SIMULATED_PENDING_OBJECT_CANNOT_CLEANUP")
            self.db.execute("DELETE FROM effects WHERE object_id=?", (object_id,))
            self.db.execute("UPDATE tasks SET cleaned=1 WHERE object_id=?", (object_id,))
        return {"absent": True, **provider_contract_metadata()}


class SimulationServer(HTTPServer):
    """单线程独立HTTP服务，控制授权与提交授权严格分离。"""

    def __init__(self, store: SimulationStore, key: str, control_key: str):
        """只绑定动态loopback端口，不监听公网或继承应用生命周期。"""
        self.store, self.key, self.control_key = store, key, control_key
        self.drop_next_accept = False
        super().__init__(("127.0.0.1", 0), SimulationHandler)


class SimulationHandler(BaseHTTPRequestHandler):
    """隔离的真实HTTP入口；模拟身份不能变成供应商资格。"""

    def log_message(self, format: str, *args: object) -> None:
        """禁止默认HTTP日志输出请求地址、任务参数或授权头。"""

    def do_GET(self) -> None:
        """分别读取健康、任务、业务对象和幂等对账事实，不推进远端任务。"""
        self._dispatch()

    def do_POST(self) -> None:
        """处理提交和模拟控制入口，不直接修改本项目Runtime。"""
        self._dispatch()

    def do_DELETE(self) -> None:
        """处理本轮合成对象的清理入口。"""
        self._dispatch()

    def _dispatch(self) -> None:
        """按固定路由鉴权；丢回执只关闭真实HTTP连接，不伪造应用状态。"""
        try:
            status, packet = self._route()
            if self.command == "POST" and self.path == "/simulation/tasks" and self.server.drop_next_accept:
                self.server.drop_next_accept = False
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
        except SimulationRejected as exc:
            status, packet = exc.status, {
                "error": {"code": exc.code}, **provider_contract_metadata(),
            }
        self._reply(status, packet)

    def _authorize(self, *, control: bool = False) -> None:
        """提交key不能访问控制接口，两类秘密不进入URL或持久业务账本。"""
        header = "X-Simulation-Control-Key" if control else "X-Simulation-Key"
        expected = self.server.control_key if control else self.server.key
        if not hmac.compare_digest(self.headers.get(header) or "", expected):
            raise SimulationRejected(401, "SIMULATED_AUTHORIZATION_REQUIRED")

    def _body(self) -> dict:
        """只读有上限的JSON对象，不接受分块、超长或未知正文。"""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 4096 or self.headers.get("Transfer-Encoding"):
                raise ValueError
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError
            return body
        except (ValueError, TypeError) as exc:
            raise SimulationRejected(400, "SIMULATED_JSON_BODY_REQUIRED") from exc

    def _route(self) -> tuple[int, dict]:
        """真实请求分别访问任务、控制、业务读回与清理，poll保持原无授权GET。"""
        store = self.server.store
        if self.command == "GET" and self.path == "/simulation/health":
            return 200, {
                **provider_contract_metadata(), "model_api_installed": False, "pid": os.getpid(),
            }
        if self.command == "POST" and self.path == "/simulation/tasks":
            self._authorize()
            return 202, store.submit(self._body(), self.headers.get("Idempotency-Key") or "")
        task_match = re.fullmatch(r"/simulation/tasks/(f3-task-[0-9a-f]{32})", self.path)
        if self.command == "GET" and task_match:
            return 200, store.status(task_match[1])
        object_match = re.fullmatch(r"/simulation/objects/(f3-obj-[0-9a-f]{32})", self.path)
        if object_match and self.command in {"GET", "DELETE"}:
            self._authorize()
            return 200, (store.readback(object_match[1]) if self.command == "GET"
                         else store.cleanup(object_match[1]))
        reconcile_match = re.fullmatch(r"/simulation/reconcile/([0-9a-f]{64})", self.path)
        if self.command == "GET" and reconcile_match:
            self._authorize()
            return 200, store.reconcile(reconcile_match[1])
        if self.command == "POST" and self.path.startswith("/simulation/control/"):
            self._authorize(control=True)
            body = self._body()
            if self.path == "/simulation/control/fault" and body == {"mode": "drop_after_accept"}:
                self.server.drop_next_accept = True
                return 200, {"fault_armed": True, **provider_contract_metadata()}
            control_match = re.fullmatch(r"/simulation/control/(f3-task-[0-9a-f]{32})/(complete|cancel)",
                                        self.path)
            if control_match and not body:
                return 200, store.control(control_match[1], control_match[2])
        raise SimulationRejected(404, "SIMULATED_ROUTE_NOT_FOUND")

    def _reply(self, status: int, value: dict) -> None:
        """输出合成业务JSON，不缓存且不记录授权；所有响应保持模拟身份。"""
        packet = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(packet)))
        self.end_headers()
        self.wfile.write(packet)


def main() -> int:
    """只启动自有700目录中的loopback进程；授权值仅从私有环境读取。"""
    parser = argparse.ArgumentParser(description="隔离异步模拟服务；不是供应商sandbox")
    parser.add_argument("--runtime", type=Path, required=True)
    args = parser.parse_args()
    runtime = args.runtime
    if (not runtime.is_absolute() or runtime.resolve() != runtime or not runtime.is_dir()
            or runtime.stat().st_uid != os.getuid() or runtime.stat().st_mode & 0o077
            or not runtime.name.startswith("f3-simulation-")):
        raise SystemExit("F3_SIMULATION_OWNED_PRIVATE_DIRECTORY_REQUIRED")
    key = os.environ.get("F3_SIMULATION_KEY", "")
    control_key = os.environ.get("F3_SIMULATION_CONTROL_KEY", "")
    namespace = os.environ.get("F3_SIMULATION_NAMESPACE", "")
    if (len(key) < 32 or len(control_key) < 32 or key == control_key
            or not re.fullmatch(r"f3-sim-[0-9a-f]{16}", namespace)):
        raise SystemExit("F3_SIMULATION_PRIVATE_CONFIGURATION_REQUIRED")
    os.umask(0o077)
    try:
        store = SimulationStore(runtime, namespace)
        server = SimulationServer(store, key, control_key)
        _write_private(runtime / "ready.private.json", {
            "pid": os.getpid(), "port": server.server_port,
            **provider_contract_metadata()})
    except (ValueError, OSError, sqlite3.Error):
        raise SystemExit("F3_SIMULATION_PRIVATE_RUNTIME_REFUSED") from None
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        store.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
