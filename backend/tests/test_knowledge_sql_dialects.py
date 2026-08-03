from sqlalchemy.dialects import mysql, sqlite

from app.api.knowledge import _bucket_chunk_statement, _document_bucket_statement


def compile_sql(statement, dialect) -> str:
    return str(
        statement.compile(dialect=dialect, compile_kwargs={"literal_binds": True})
    ).upper()


def test_mysql_knowledge_statements_do_not_cast_text_as_blob() -> None:
    statements = (
        _document_bucket_statement("mysql", "tenant_demo", "doc_1"),
        _bucket_chunk_statement("mysql", "tenant_demo", "bucket_1"),
    )

    for statement in statements:
        sql = compile_sql(statement, mysql.dialect())
        assert " AS BLOB" not in sql
        assert "CAST(" not in sql


def test_sqlite_legacy_statements_keep_blob_casts() -> None:
    statements = (
        _document_bucket_statement("sqlite", "tenant_demo", "doc_1"),
        _bucket_chunk_statement("sqlite", "tenant_demo", "bucket_1"),
    )

    for statement in statements:
        assert " AS BLOB" in compile_sql(statement, sqlite.dialect())
