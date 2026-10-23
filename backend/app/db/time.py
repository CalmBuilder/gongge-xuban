"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : time.py
@CallChain  : MemoryCapture worker → 数据库 UTC 时间 → 截止/租约/CAS
@Description: 沿现有 Store 的数据库时间边界判定跨进程租约，不创建业务执行实例。
"""

from datetime import datetime

from sqlalchemy import func
from sqlmodel import Session, select


def database_utc_now(db: Session) -> datetime:
    """返回数据库权威 UTC，MySQL 显式 UTC 且保留微秒，SQLite 沿 UTC timestamp。"""

    with db.no_autoflush:
        clock = (
            func.utc_timestamp(6)
            if db.get_bind().dialect.name == "mysql"
            else func.current_timestamp()
        )
        value = db.exec(select(clock)).one()
    if not isinstance(value, datetime):
        # MySQL 通用 func 无 SQLAlchemy DateTime 返回适配，驱动返回字符串时严格解析。
        value = datetime.fromisoformat(str(value))
    return value.replace(tzinfo=None)
