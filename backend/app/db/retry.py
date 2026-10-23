"""
@Time       : 2026/10/10
@Author     : zhanglp8181
@File       : retry.py
@CallChain  : FastAPI只读聚合 → MySQL短事务死锁 → 回滚并重读一次
@Description: 为天然幂等读取提供仅针对MySQL 1213的有界事务恢复。
"""

from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.exc import OperationalError
from sqlmodel import Session


Result = TypeVar("Result")


def retry_mysql_deadlock_once(session: Session, operation: Callable[[], Result]) -> Result:
    """仅对 MySQL 死锁 1213 回滚整笔事务并重新执行一次幂等读取。"""
    try:
        return operation()
    except OperationalError as error:
        if session.get_bind().dialect.name != "mysql" or not _is_mysql_deadlock(error):
            raise
        session.rollback()
    return operation()


def _is_mysql_deadlock(error: OperationalError) -> bool:
    """从驱动原异常读取精确错误码，避免重试锁超时或其他数据库故障。"""
    original = error.orig
    code = getattr(original, "errno", None)
    if code is None:
        arguments = getattr(original, "args", ())
        code = arguments[0] if arguments else None
    return type(code) is int and code == 1213
