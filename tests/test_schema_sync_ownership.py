from __future__ import annotations

import unittest

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy import inspect

from app.common.schema_sync import (
    SCHEMA_OWNER_INFO_KEY,
    bootstrap_metadata_projection,
    bootstrap_managed_tables,
    migration_managed_schema_info,
    sync_missing_columns,
    sync_missing_indexes,
    table_has_migration_managed_schema,
)
from migrations.ownership import include_migration_managed_object


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

    def test_bootstrap_projection_keeps_mixed_tables_and_omits_managed_children(
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
                "parent_id",
                Integer,
                ForeignKey("schema_owner_parent.id"),
                nullable=False,
            ),
            Column(
                "bootstrap_value",
                String(50),
                nullable=False,
                server_default="bootstrap",
            ),
            Column(
                "migration_value",
                String(50),
                info=migration_managed_schema_info(),
            ),
            UniqueConstraint("bootstrap_value", name="uq_owner_bootstrap_value"),
            CheckConstraint(
                "length(bootstrap_value) > 0",
                name="ck_owner_bootstrap_value",
            ),
        )
        Table(
            "schema_owner_parent",
            metadata,
            Column("id", Integer, primary_key=True),
        )
        indexed = Table(
            "schema_owner_migration_index_table",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("bootstrap_value", String(50)),
            Column("migration_value", String(50)),
        )
        Index(
            "ix_owner_migration_create",
            indexed.c.migration_value,
            info=migration_managed_schema_info(),
        )
        Index("ix_owner_bootstrap_create", indexed.c.bootstrap_value)

        auto_tables = bootstrap_managed_tables(metadata)
        bootstrap_metadata_projection(metadata).create_all(self.engine)

        self.assertEqual(
            {table.name for table in auto_tables},
            {
                "schema_owner_bootstrap_table",
                "schema_owner_migration_column_table",
                "schema_owner_migration_index_table",
                "schema_owner_parent",
            },
        )
        inspector = inspect(self.engine)
        self.assertTrue(inspector.has_table("schema_owner_bootstrap_table"))
        self.assertFalse(inspector.has_table("schema_owner_migration_table"))
        self.assertTrue(
            inspector.has_table("schema_owner_migration_column_table")
        )
        mixed_columns = {
            column["name"]
            for column in inspector.get_columns("schema_owner_migration_column_table")
        }
        self.assertTrue({"id", "parent_id", "bootstrap_value"} <= mixed_columns)
        self.assertNotIn("migration_value", mixed_columns)
        self.assertEqual(
            inspector.get_pk_constraint("schema_owner_migration_column_table")[
                "constrained_columns"
            ],
            ["id"],
        )
        self.assertTrue(
            next(
                column
                for column in inspector.get_columns(
                    "schema_owner_migration_column_table"
                )
                if column["name"] == "bootstrap_value"
            )["default"]
        )
        self.assertTrue(
            any(
                foreign_key["referred_table"] == "schema_owner_parent"
                for foreign_key in inspector.get_foreign_keys(
                    "schema_owner_migration_column_table"
                )
            )
        )
        self.assertTrue(
            any(
                constraint["name"] == "uq_owner_bootstrap_value"
                for constraint in inspector.get_unique_constraints(
                    "schema_owner_migration_column_table"
                )
            )
        )
        self.assertTrue(
            any(
                constraint["name"] == "ck_owner_bootstrap_value"
                for constraint in inspector.get_check_constraints(
                    "schema_owner_migration_column_table"
                )
            )
        )
        indexes = {
            index["name"]
            for index in inspector.get_indexes("schema_owner_migration_index_table")
        }
        self.assertIn("ix_owner_bootstrap_create", indexes)
        self.assertNotIn("ix_owner_migration_create", indexes)

    def test_alembic_traverses_mixed_table_but_only_includes_managed_children(
        self,
    ) -> None:
        metadata = MetaData()
        table = Table(
            "schema_owner_alembic_mixed",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("bootstrap_value", String(50)),
            Column(
                "migration_value",
                String(50),
                info=migration_managed_schema_info(),
            ),
        )
        bootstrap_index = Index("ix_owner_alembic_bootstrap", table.c.bootstrap_value)
        migration_index = Index(
            "ix_owner_alembic_migration",
            table.c.migration_value,
            info=migration_managed_schema_info(),
        )

        self.assertTrue(table_has_migration_managed_schema(table))
        self.assertTrue(
            include_migration_managed_object(table, table.name, "table", False, None)
        )
        self.assertFalse(
            include_migration_managed_object(
                table.c.bootstrap_value,
                "bootstrap_value",
                "column",
                False,
                None,
            )
        )
        self.assertTrue(
            include_migration_managed_object(
                table.c.migration_value,
                "migration_value",
                "column",
                False,
                None,
            )
        )
        self.assertFalse(
            include_migration_managed_object(
                bootstrap_index,
                bootstrap_index.name,
                "index",
                False,
                None,
            )
        )
        self.assertTrue(
            include_migration_managed_object(
                migration_index,
                migration_index.name,
                "index",
                False,
                None,
            )
        )

    def test_projection_rejects_bootstrap_constraint_on_managed_column(
        self,
    ) -> None:
        metadata = MetaData()
        Table(
            "schema_owner_invalid_constraint",
            metadata,
            Column("id", Integer, primary_key=True),
            Column(
                "migration_value",
                String(50),
                info=migration_managed_schema_info(),
            ),
            UniqueConstraint(
                "migration_value",
                name="uq_owner_invalid_migration_value",
            ),
        )

        with self.assertRaisesRegex(
            ValueError,
            "Bootstrap constraint references migration-managed column",
        ):
            bootstrap_metadata_projection(metadata)

    def test_projection_rejects_unmarked_index_on_managed_column(self) -> None:
        metadata = MetaData()
        table = Table(
            "schema_owner_invalid_index",
            metadata,
            Column("id", Integer, primary_key=True),
            Column(
                "migration_value",
                String(50),
                info=migration_managed_schema_info(),
            ),
        )
        Index("ix_owner_invalid_migration_value", table.c.migration_value)

        with self.assertRaisesRegex(
            ValueError,
            "Bootstrap index references migration-managed column",
        ):
            bootstrap_metadata_projection(metadata)

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
