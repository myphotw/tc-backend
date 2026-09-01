"""Shared helpers for the opt-in PostgreSQL migration integration suite."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from migrations.baseline import BASELINE_REQUIRED_TABLES


TEST_ALEMBIC_ROOT = Path(__file__).resolve().parent / "alembic"


def build_test_alembic_config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(TEST_ALEMBIC_ROOT))
    config.attributes["connection"] = connection
    return config


def create_legacy_schema(engine: Engine) -> None:
    """Create the current pre-Alembic model schema without a version table."""
    from app.common.model_registry import Base

    Base.metadata.create_all(bind=engine)


def public_catalog_snapshot(connection: Connection) -> tuple[tuple[object, ...], ...]:
    """Capture public schema objects for read-only side-effect comparisons."""
    rows = connection.execute(
        text(
            """
            SELECT 'relation', c.relkind::text, c.relname, ''
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
            UNION ALL
            SELECT
                'column',
                c.relname,
                a.attname,
                pg_catalog.format_type(a.atttypid, a.atttypmod)
                    || ':' || a.attnotnull::text
                    || ':' || a.attidentity::text
                    || ':' || a.attgenerated::text
            FROM pg_catalog.pg_attribute AS a
            JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND a.attnum > 0
              AND NOT a.attisdropped
            UNION ALL
            SELECT
                'constraint',
                c.relname,
                con.conname,
                pg_catalog.pg_get_constraintdef(con.oid, true)
            FROM pg_catalog.pg_constraint AS con
            JOIN pg_catalog.pg_class AS c ON c.oid = con.conrelid
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
            ORDER BY 1, 2, 3, 4
            """
        )
    ).all()
    return tuple(tuple(row) for row in rows)


def application_schema_snapshot(
    connection: Connection,
) -> tuple[tuple[object, ...], ...]:
    """Snapshot only immutable baseline application structure, not Alembic."""
    inspector = inspect(connection)
    snapshot: list[tuple[object, ...]] = []
    for table_name in sorted(BASELINE_REQUIRED_TABLES):
        columns = inspector.get_columns(table_name, schema="public")
        snapshot.extend(
            (
                "column",
                table_name,
                column["name"],
                str(column["type"]),
                bool(column.get("nullable")),
                str(column.get("default")),
            )
            for column in columns
        )
        primary_key = inspector.get_pk_constraint(table_name, schema="public")
        snapshot.append(
            (
                "primary_key",
                table_name,
                tuple(primary_key.get("constrained_columns") or ()),
            )
        )
        snapshot.extend(
            (
                "foreign_key",
                table_name,
                tuple(foreign_key.get("constrained_columns") or ()),
                foreign_key.get("referred_schema"),
                foreign_key.get("referred_table"),
                tuple(foreign_key.get("referred_columns") or ()),
            )
            for foreign_key in inspector.get_foreign_keys(
                table_name,
                schema="public",
            )
        )
        snapshot.extend(
            (
                "unique",
                table_name,
                tuple(constraint.get("column_names") or ()),
            )
            for constraint in inspector.get_unique_constraints(
                table_name,
                schema="public",
            )
        )
        snapshot.extend(
            (
                "index",
                table_name,
                index.get("name"),
                tuple(index.get("column_names") or ()),
                bool(index.get("unique")),
            )
            for index in inspector.get_indexes(table_name, schema="public")
        )
    return tuple(sorted(snapshot, key=repr))


def table_exists(connection: Connection, table_name: str) -> bool:
    return inspect(connection).has_table(table_name, schema="public")


def try_migration_lock(connection: Connection, lock_key: int) -> bool:
    acquired = bool(
        connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": lock_key},
        ).scalar_one()
    )
    connection.commit()
    return acquired


def release_migration_lock(connection: Connection, lock_key: int) -> bool:
    if connection.in_transaction():
        connection.rollback()
    released = bool(
        connection.execute(
            text("SELECT pg_advisory_unlock(:key)"),
            {"key": lock_key},
        ).scalar_one()
    )
    connection.commit()
    return released


def run_with_engine_patch(
    migration_engine_factory: Callable[[], Engine],
    operation: Callable[[], object],
) -> object:
    from unittest.mock import patch

    with patch(
        "scripts.db_migrate.create_migration_engine",
        side_effect=migration_engine_factory,
    ):
        return operation()
