"""
@Time       : 2026/10/01
@Author     : zhanglp8181
@File       : test_memory_projection_mysql.py
@CallChain  : pytest mysql → 随机数据库/Alembic → 冻结披露/撤销SDK合同
@Description: 使用真实MySQL验证与SQLite同一投影及代数行为，不修改真实租户库。
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlmodel import create_engine

from tests.test_memory_projection_records import check_generation_contract

pytestmark = pytest.mark.mysql


def test_mysql_frozen_disclosure_clear_revoke_new_source(mysql_database_url: str) -> None:
    """先Alembic升级随机MySQL库，再执行相同完整快照/代数/外发断言。"""
    backend = Path(__file__).resolve().parents[2]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    config.attributes["database_url"] = mysql_database_url
    command.upgrade(config, "head")
    engine = create_engine(mysql_database_url, pool_pre_ping=True)
    try:
        check_generation_contract(engine)
    finally:
        engine.dispose()
