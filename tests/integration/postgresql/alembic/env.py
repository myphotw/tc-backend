"""Test-only Alembic environment; never part of the production revision graph."""

from alembic import context


config = context.config
connection = config.attributes.get("connection")
if connection is None:
    raise RuntimeError("test Alembic environment requires an injected connection")

context.configure(
    connection=connection,
    transaction_per_migration=True,
    version_table="test_alembic_version",
    version_table_schema="public",
)

with context.begin_transaction():
    context.run_migrations()

