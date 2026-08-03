from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

import app.db.models  # noqa: F401
from app.config import get_settings


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

configured_url = str(
    config.attributes.get("database_url") or get_settings().database_url
).replace("%", "%%")
config.set_main_option("sqlalchemy.url", configured_url)
target_metadata = SQLModel.metadata


def include_object(
    object_, name: str | None, type_: str, reflected: bool, compare_to: object | None
) -> bool:
    del object_, name
    return not (type_ == "table" and reflected and compare_to is None)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
