"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : final_f2_h3_fullstack_supervisor.py
@CallChain  : H3 Playwright → 原应用子进程 → 本次同库真PID重启
@Description: 只处理自有进程重启，不改变任何执行、候选、Signal或发布状态。
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main() -> None:
    """守护本次H3服务，同库新PID保留旧实例和job丢失的真实恢复边界。"""
    control = Path(os.environ["FULLSTACK_SD6B_CONTROL_DIR"])
    environment = os.environ.copy()
    command = [sys.executable, str(Path(__file__).with_name("start_final_f2_h3_server.py"))]
    child = subprocess.Popen(command, env=environment)

    def stop(_signum, _frame):
        """只停止当前自有子进程，并交由finally核退出。"""
        child.terminate()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while child.poll() is None:
            request = control / "restart.request"
            if request.exists():
                request.unlink()
                child.terminate()
                child.wait(timeout=15)
                environment["FULLSTACK_E2E_REUSE_RUNTIME"] = "1"
                child = subprocess.Popen(command, env=environment)
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
