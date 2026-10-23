"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : start_final_f2_server.py
@CallChain  : F2 Playwright → 自有服务 → 原API/Runtime/SDK
@Description: 复用F1合法资源及受控远端，附加F2身份资源；同库重启不重置业务事实。
"""
from __future__ import annotations

import os
import shutil

from start_fullstack_server import (
    E2E_PORT,
    E2E_RUNTIME_DIR,
    configure_environment,
    install_sd6b_browser_controls,
)


def main() -> None:
    """只在显式自有运行中启动；新建和真重启沿原数据库迁移及应用生命周期。"""
    if os.environ.get("FULLSTACK_FINAL_F2") != "1":
        raise RuntimeError("F2_EXPLICIT_ISOLATION_REQUIRED")
    reuse = os.environ.get("FULLSTACK_E2E_REUSE_RUNTIME") == "1"
    database = E2E_RUNTIME_DIR / "e2e.sqlite3"
    if reuse and not os.environ.get("FULLSTACK_E2E_DATABASE_URL") and not database.is_file():
        raise RuntimeError("F2_EXISTING_DATABASE_REQUIRED")
    if not reuse:
        shutil.rmtree(E2E_RUNTIME_DIR, ignore_errors=True)
        E2E_RUNTIME_DIR.mkdir(mode=0o700)
    configure_environment(database)
    os.environ["DYNAMIC_TASK_EXTERNAL_WRITE_AGENT_ALLOWLIST"] = "agent_f1_dta"
    from final_f1_fullstack_fixture import prepare_final_f1
    from final_f2_fullstack_fixture import prepare_final_f2

    prepare_final_f1(E2E_RUNTIME_DIR, reuse=reuse)
    prepare_final_f2(reuse=reuse)
    if os.environ.get("FULLSTACK_F2_JOURNEY") == "JRN06":
        from final_f2_h2_fixture import prepare_h2

        prepare_h2(reuse=reuse)
    install_sd6b_browser_controls()
    import uvicorn
    from single_port_app import app

    uvicorn.run(app, host="127.0.0.1", port=E2E_PORT, log_level="warning")


if __name__ == "__main__":
    main()
