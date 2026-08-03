from sqlalchemy import String, Text
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlmodel.sql.sqltypes import AutoString

from app.db.models import AgentResourceBinding, AgentSkillBranchVersion, Message, PersonaConfig


def test_high_risk_composite_index_columns_have_bounded_lengths() -> None:
    branch = AgentSkillBranchVersion.__table__.c
    resource = AgentResourceBinding.__table__.c

    assert [branch[name].type.length for name in ("tenant_id", "agent_id", "skill_id")] == [
        128,
        128,
        128,
    ]
    assert branch.version.type.length == 64
    assert [
        resource[name].type.length for name in ("tenant_id", "agent_id", "resource_id")
    ] == [128, 128, 128]
    assert resource.resource_type.type.length == 64


def test_long_content_uses_mysql_longtext_and_sqlite_text() -> None:
    for column in (Message.__table__.c.content, PersonaConfig.__table__.c.system_prompt):
        assert isinstance(column.type.dialect_impl(mysql.dialect()), mysql.LONGTEXT)
        assert isinstance(column.type.dialect_impl(sqlite.dialect()), Text)


def test_all_string_columns_are_explicitly_sized_or_text() -> None:
    from sqlmodel import SQLModel

    for table in SQLModel.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, (String, AutoString)) and not isinstance(column.type, Text):
                assert column.type.length is not None, (
                    f"{table.name}.{column.name} has implicit VARCHAR"
                )


def test_every_table_and_index_compiles_for_mysql() -> None:
    from sqlmodel import SQLModel

    dialect = mysql.dialect()
    for table in SQLModel.metadata.sorted_tables:
        assert str(CreateTable(table).compile(dialect=dialect))
        for index in table.indexes:
            assert str(CreateIndex(index).compile(dialect=dialect))
