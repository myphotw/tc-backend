"""Alembic environment for explicit, operator-run TC-Backend migrations."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from app.common.model_registry import Base
from migrations.ownership import include_migration_managed_object


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _configure_context(**kwargs: object) -> None:
    context.configure(
        target_metadata=target_metadata,
        include_schemas=False,
        include_object=include_migration_managed_object,
        compare_type=True,
        compare_server_default=False,
        transaction_per_migration=True,
        version_table="alembic_version",
        version_table_schema="public",
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Render SQL without opening a database connection."""
    _configure_context(
        url="postgresql://",
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run only with the connection supplied by the guarded CLI wrapper."""
    connection = config.attributes.get("connection")
    if connection is None:
        raise RuntimeError(
            "Online migrations must run through scripts/db_migrate.py; "
            "direct Alembic online execution is disabled"
        )

    _configure_context(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
