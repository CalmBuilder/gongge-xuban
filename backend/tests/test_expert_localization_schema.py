from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from sqlalchemy import Text
from sqlalchemy.dialects import mysql, sqlite

from app.db.models import AgentProfile


def test_agent_profile_has_nullable_original_language_columns() -> None:
    columns = AgentProfile.__table__.c
    assert columns.original_name.nullable is True
    assert columns.original_name.type.length == 191
    assert columns.original_locale.nullable is True
    assert columns.original_locale.type.length == 64
    assert isinstance(
        columns.original_description.type.dialect_impl(mysql.dialect()),
        mysql.MEDIUMTEXT,
    )
    assert isinstance(
        columns.original_persona_prompt.type.dialect_impl(mysql.dialect()),
        mysql.LONGTEXT,
    )
    assert isinstance(
        columns.original_persona_prompt.type.dialect_impl(sqlite.dialect()), Text
    )


def test_original_language_migration_follows_mysql_baseline() -> None:
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260718_0002_agent_profile_original_fields.py"
    )
    spec = spec_from_file_location("original_fields_migration", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "20260718_0002"
    assert module.down_revision == "20260718_0001"
