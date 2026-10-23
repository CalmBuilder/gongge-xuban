"""
@Time       : 2026/10/04
@Author     : zhanglp8181
@File       : final_f3_sdk_handoff.py
@CallChain  : F3自有LLMClient → 原取消/stream线程 → 脱敏旁证元数据 → 最终SDK
@Description: 只跨线程传递F3观察元数据；原模型ContextVar、取消、Thread对象和调用结果保持。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from final_f3_sdk_identity import bind_observation_handoff, sdk_identity


@contextmanager
def sdk_thread_handoff(module) -> Iterator[None]:
    """仅在自有测试进程包装原LLMClient线程入口，退出恢复原符号；不改生产源码。"""
    original_run = module.run_cancellable
    original_thread = module.Thread

    def run_cancellable(operation, *args, **kwargs):
        """捕获调用方安全旁证，原取消函数仍负责原超时/关闭/异常和线程执行。"""
        metadata = sdk_identity()

        def observed_operation():
            """只绑定观察器私有上下文，原模型阶段仍按原线程行为读取。"""
            with bind_observation_handoff(metadata):
                return operation()

        return original_run(observed_operation, *args, **kwargs)

    def thread(*args, **kwargs):
        """原stream及关闭线程构造参数仅替换观察target，返回同一原Thread类型对象。"""
        metadata = sdk_identity()
        arguments = list(args)
        target = kwargs.get("target", arguments[1] if len(arguments) > 1 else None)
        if callable(target):
            def observed_target(*target_args, **target_kwargs):
                """原target实参和结果保持；只在执行期间提供安全身份摘要。"""
                with bind_observation_handoff(metadata):
                    return target(*target_args, **target_kwargs)

            if "target" in kwargs or len(arguments) <= 1:
                kwargs["target"] = observed_target
            else:
                arguments[1] = observed_target
        return original_thread(*arguments, **kwargs)

    module.run_cancellable = run_cancellable
    module.Thread = thread
    try:
        yield
    finally:
        if module.run_cancellable is run_cancellable:
            module.run_cancellable = original_run
        if module.Thread is thread:
            module.Thread = original_thread
