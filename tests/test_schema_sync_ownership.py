from __future__ import annotations

import unittest

from sqlalchemy import Column, Index, Integer, MetaData, String, Table, create_engine
from sqlalchemy import inspect

from app.common.schema_sync import (
    SCHEMA_OWNER_INFO_KEY,
    bootstrap_managed_tables,
    migration_managed_schema_info,
    sync_missing_columns,
    sync_missing_indexes,
)


class SchemaSyncOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_missing_bootstrap_column_is_added_and_migration_column_is_skipped(
        self,
    ) -> None:
        physical = MetaData()
        Table(
            "schema_owner_columns",
            physical,
            Column("id", Integer, primary_key=True),
        ).create(self.engine)

        model = MetaData()
        Table(
            "schema_owner_columns",
            model,
            Column("id", Integer, primary_key=True),
            Column("bootstrap_value", String(50)),
            Column(
                "migration_value",
                String(50),
                info=migration_managed_schema_info(),
            ),
        )

        changes = sync_missing_columns(self.engine, metadata=model)

        self.assertEqual(changes, ["schema_owner_columns.bootstrap_value"])
        column_names = {
            column["name"]
            for column in inspect(self.engine).get_columns("schema_owner_columns")
        }
        self.assertIn("bootstrap_value", column_names)
        self.assertNotIn("migration_value", column_names)

    def test_missing_bootstrap_index_is_added_and_migration_index_is_skipped(
        self,
    ) -> None:
        physical = MetaData()
        Table(
            "schema_owner_indexes",
            physical,
            Column("id", Integer, primary_key=True),
            Column("bootstrap_value", String(50)),
            Column("migration_value", String(50)),
        ).create(self.engine)

        model = MetaData()
        table = Table(
            "schema_owner_indexes",
            model,
            Column("id", Integer, primary_key=True),
            Column("bootstrap_value", String(50)),
            Column("migration_value", String(50)),
        )
        Index("ix_owner_bootstrap", table.c.bootstrap_value)
        Index(
            "ix_owner_migration",
            table.c.migration_value,
            info=migration_managed_schema_info(),
        )

        changes = sync_missing_indexes(self.engine, metadata=model)

        self.assertEqual(changes, ["index:ix_owner_bootstrap"])
        index_names = {
            index["name"]
            for index in inspect(self.engine).get_indexes("schema_owner_indexes")
        }
        self.assertIn("ix_owner_bootstrap", index_names)
        self.assertNotIn("ix_owner_migration", index_names)

    def test_existing_migration_managed_column_and_index_are_noop(self) -> None:
        physical = MetaData()
        physical_table = Table(
            "schema_owner_existing",
            physical,
            Column("id", Integer, primary_key=True),
            Column("migration_value", String(50)),
        )
        Index("ix_owner_existing_migration", physical_table.c.migration_value)
        physical.create_all(self.engine)

        model = MetaData()
        model_table = Table(
            "schema_owner_existing",
            model,
            Column("id", Integer, primary_key=True),
            Column(
                "migration_value",
                String(50),
                info=migration_managed_schema_info(),
            ),
        )
        Index(
            "ix_owner_existing_migration",
            model_table.c.migration_value,
            info=migration_managed_schema_info(),
        )

        self.assertEqual(sync_missing_columns(self.engine, metadata=model), [])
        self.assertEqual(sync_missing_indexes(self.engine, metadata=model), [])

    def test_create_all_scope_excludes_tables_with_migration_owned_schema(
        self,
    ) -> None:
        metadata = MetaData()
        Table(
            "schema_owner_bootstrap_table",
            metadata,
            Column("id", Integer, primary_key=True),
        )
        Table(
            "schema_owner_migration_table",
            metadata,
            Column("id", Integer, primary_key=True),
            info=migration_managed_schema_info(),
        )
        Table(
            "schema_owner_migration_column_table",
            metadata,
            Column("id", Integer, primary_key=True),
            Column(
                "migration_value",
                String(50),
                info=migration_managed_schema_info(),
            ),
        )
        indexed = Table(
            "schema_owner_migration_index_table",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("migration_value", String(50)),
        )
        Index(
            "ix_owner_migration_create",
            indexed.c.migration_value,
            info=migration_managed_schema_info(),
        )

        auto_tables = bootstrap_managed_tables(metadata)
        metadata.create_all(self.engine, tables=auto_tables)

        self.assertEqual(
            {table.name for table in auto_tables},
            {"schema_owner_bootstrap_table"},
        )
        inspector = inspect(self.engine)
        self.assertTrue(inspector.has_table("schema_owner_bootstrap_table"))
        self.assertFalse(inspector.has_table("schema_owner_migration_table"))
        self.assertFalse(
            inspector.has_table("schema_owner_migration_column_table")
        )
        self.assertFalse(inspector.has_table("schema_owner_migration_index_table"))

    def test_unknown_explicit_owner_fails_closed(self) -> None:
        metadata = MetaData()
        Table(
            "schema_owner_typo",
            metadata,
            Column("id", Integer, primary_key=True),
            info={SCHEMA_OWNER_INFO_KEY: "migraton"},
        )

        with self.assertRaisesRegex(ValueError, "Unknown schema owner"):
            bootstrap_managed_tables(metadata)


if __name__ == "__main__":
    unittest.main()
