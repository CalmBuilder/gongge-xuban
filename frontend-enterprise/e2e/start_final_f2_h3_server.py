"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : start_final_f2_h3_server.py
@CallChain  : H3自有守护 → 原F2启动/合法资源 → H3补充资源及真实SDKHTTP
@Description: 只扩展测试资源准备函数，原应用/迁移/生命周期与既有F1/H1/H2启动源码不改。
"""
import os

import final_f2_fullstack_fixture as fixture
from final_f2_h3_fixture import prepare_h3
from start_final_f2_server import main as original_main

original_prepare = fixture.prepare_final_f2


def prepare(*, reuse: bool) -> None:
    """沿原F2准备合法身份后附加H3；重启不重种原事实。"""
    original_prepare(reuse=reuse)
    prepare_h3(reuse=reuse)


def main() -> None:
    """明确H3隔离标记才扩展测试准备，生产启动不可调用。"""
    if os.environ.get("FULLSTACK_F2_JOURNEY") != "JRN07":
        raise RuntimeError("F2_H3_JOURNEY_REQUIRED")
    fixture.prepare_final_f2 = prepare
    original_main()


if __name__ == "__main__":
    main()
