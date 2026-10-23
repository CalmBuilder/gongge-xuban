"""
@Time       : 2026/10/02
@Author     : zhanglp8181
@File       : keys.py
@CallChain  : 记忆worker结算/内部候选/冻结验证 → 统一持久规范键
@Description: 集中原worker的casefold与空白折叠规则，不改正文或回填历史。
"""


def normalize_memory_key_identity(value: object) -> str:
    """沿原持久键语义归一大小写及Unicode空白，保留标点与词边界。"""
    return " ".join(str(value or "").strip().casefold().split())
