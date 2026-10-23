"""
@Time       : 2026/10/10
@Author     : zhanglp8181
@File       : test_mysql_deadlock_retry.py
@CallChain  : pytest → MySQL 1213事务恢复助手 → 回滚、精确重试和错误传播
@Description: 验证死锁重读只重试一次且不吞并其他方言或数据库错误。
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pymysql.err import OperationalError as DriverOperationalError
from sqlalchemy.exc import OperationalError

from app.db.retry import retry_mysql_deadlock_once


def _mysql_error(code: int) -> OperationalError:
    """构造驱动带整数错误码的 SQLAlchemy 运行错误。"""
    return OperationalError("SELECT 1", {}, DriverOperationalError(code, "synthetic"))


def _session(dialect: str) -> Mock:
    """构造只含方言身份和事务回滚观察器的隔离会话替身。"""
    session = Mock()
    session.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
    return session


def test_mysql_deadlock_rolls_back_and_repeats_operation_once() -> None:
    """精确 1213 必须先回滚被中止事务，再完整重放一次读取。"""
    session = _session("mysql")
    attempts = 0

    def operation() -> str:
        """首次模拟死锁，第二次返回读取结果。"""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _mysql_error(1213)
        return "read-result"

    assert retry_mysql_deadlock_once(session, operation) == "read-result"
    assert attempts == 2
    session.rollback.assert_called_once_with()


@pytest.mark.parametrize(("dialect", "code"), [("sqlite", 1213), ("mysql", 1205)])
def test_non_mysql_or_non_deadlock_error_propagates_without_retry(
    dialect: str,
    code: int,
) -> None:
    """其他方言和锁等待超时原样传播，不扩大静默恢复范围。"""
    session = _session(dialect)
    attempts = 0

    def operation() -> None:
        """记录单次调用并抛出指定的非目标数据库错误。"""
        nonlocal attempts
        attempts += 1
        raise _mysql_error(code)

    with pytest.raises(OperationalError):
        retry_mysql_deadlock_once(session, operation)
    assert attempts == 1
    session.rollback.assert_not_called()


def test_repeated_mysql_deadlock_propagates_after_one_retry() -> None:
    """第二次仍死锁时停止重试并传播原错误，保证恢复次数有界。"""
    session = _session("mysql")
    attempts = 0

    def operation() -> None:
        """每次读取均模拟原生 1213，供验证最多执行两次。"""
        nonlocal attempts
        attempts += 1
        raise _mysql_error(1213)

    with pytest.raises(OperationalError):
        retry_mysql_deadlock_once(session, operation)
    assert attempts == 2
    session.rollback.assert_called_once_with()
