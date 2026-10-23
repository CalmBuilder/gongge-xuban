"""
@Time       : 2026/10/05
@Author     : zhanglp8181
@File       : final_f3_live_supervisor.py
@CallChain  : 自有Playwright webServer → 原应用子进程 → 文件请求真重启
@Description: 仅管理本轮真实模型应用PID；重启复用原库和配置，不重种任务事实。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from start_final_f3_live_server import read_private


def stop_owned(child: subprocess.Popen) -> None:
    """仅停止自己Popen创建的子进程；超时回收该PID，不按端口查杀服务。"""
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def main() -> int:
    """监听自有control文件，真实新PID继续原数据库，不改变远端任务和已批准参数。"""
    if os.environ.get("FULLSTACK_FINAL_F3_LIVE") != "1":
        raise RuntimeError("F3_LIVE_SUPERVISOR_SCOPE_REQUIRED")
    runtime = Path(os.environ["F3_LIVE_RUNTIME"])
    configuration = read_private(Path(os.environ["F3_LIVE_CONFIGURATION_PATH"]))
    control = runtime / "control"
    control.mkdir(mode=0o700, exist_ok=True)
    environment = os.environ.copy()
    starter = Path(__file__).with_name("start_final_f3_live_server.py")
    child = subprocess.Popen([sys.executable, str(starter)], env=environment)

    def record_pid() -> None:
        """记录应用PID、进程组、session与starttime，供重启和安全清理绑定身份。"""
        target = control / "server-pid.safe.json"
        fields = (Path("/proc") / str(child.pid) / "stat").read_text().rsplit(")", 1)[1].split()
        if len(fields) <= 19:
            raise RuntimeError("F3_LIVE_APPLICATION_PROCESS_IDENTITY_REQUIRED")
        target.write_text(json.dumps({
            "pid": child.pid, "run_id": configuration["run_id"],
            "process_group_id": os.getpgid(child.pid), "session_id": os.getsid(child.pid),
            "start_ticks": int(fields[19]),
        }))
        target.chmod(0o600)

    def stop(_signum, _frame) -> None:
        """父运行结束只回收当前自有应用，供应商由外层独立拥有者管理。"""
        stop_owned(child)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    record_pid()
    try:
        while child.poll() is None:
            request = control / "restart.request"
            if request.exists():
                request.unlink()
                previous_pid = child.pid
                stop_owned(child)
                environment["F3_LIVE_REUSE"] = "1"
                child = subprocess.Popen([sys.executable, str(starter)], env=environment)
                record_pid()
                complete = control / "restart.done"
                complete.write_text(json.dumps({"previous_pid": previous_pid, "pid": child.pid}))
                complete.chmod(0o600)
            time.sleep(0.1)
        return child.returncode
    finally:
        stop_owned(child)


if __name__ == "__main__":
    raise SystemExit(main())
