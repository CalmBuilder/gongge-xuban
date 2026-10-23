"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : sd6b_fullstack_supervisor.py
@CallChain  : Playwright → 隔离子进程全栈服务 → 重启 → 持久记忆扫描
@Description: 通过文件指令重启仅本次测试服务，保留隔离数据库，不干预任务事实。
"""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main() -> None:
    """守护测试子进程，重启只复用本次随机 MySQL 或临时 SQLite，退出必回收。"""

    control = Path(os.environ["FULLSTACK_SD6B_CONTROL_DIR"])
    control.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    child = subprocess.Popen([sys.executable, str(Path(__file__).with_name("start_fullstack_server.py"))], env=environment)
    def stop(_signum, _frame):
        """停止当前测试子进程后退出守护器，不触碰仓库或生产服务。"""
        child.terminate()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while child.poll() is None:
            command = control / "restart.request"
            if command.exists():
                command.unlink()
                child.terminate()
                child.wait(timeout=15)
                environment["FULLSTACK_E2E_REUSE_RUNTIME"] = "1"
                child = subprocess.Popen([sys.executable, str(Path(__file__).with_name("start_fullstack_server.py"))], env=environment)
                (control / "restart.done").touch()
            time.sleep(0.1)
        raise SystemExit(child.returncode)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    main()
