from __future__ import annotations

import ast
from pathlib import Path
import unittest

from sqlalchemy import Column, Index, Integer, MetaData, String, Table

from app.common.schema_sync import (
    bootstrap_metadata_projection,
    migration_managed_schema_info,
)
from migrations.ownership import include_migration_managed_object
from scripts.db_migrate import (
    BASELINE_REVISION,
    MigrationLockUnavailable,
    MigrationVerificationError,
    _verify_ownership_projection,
    build_alembic_config,
    mask_secrets,
    require_single_head,
    run_locked_operation,
    session_advisory_lock,
    verify_revision_graph,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = (
    PROJECT_ROOT
    / "migrations"
    / "versions"
    / "20260831_0001_baseline.py"
)


class _ScalarResult:
    def __init__(self, value: bool) -> None:
        self.value = value

    def scalar_one(self) -> bool:
        return self.value


class _FakeConnection:
    def __init__(self, results: list[bool]) -> None:
        self.results = list(results)
        self.statements: list[tuple[str, dict[str, int]]] = []
        self.transaction_open = False
        self.commit_count = 0
        self.rollback_count = 0

    def execute(self, statement, parameters):
        self.transaction_open = True
        self.statements.append((str(statement), parameters))
        return _ScalarResult(self.results.pop(0))

    def in_transaction(self) -> bool:
        return self.transaction_open

    def commit(self) -> None:
        self.commit_count += 1
        self.transaction_open = False

    def rollback(self) -> None:
        self.rollback_count += 1
        self.transaction_open = False


class DatabaseMigrationFrameworkTests(unittest.TestCase):
    def test_baseline_revision_is_noop_in_both_directions(self) -> None:
        tree = ast.parse(BASELINE_PATH.read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }

        for function_name in ("upgrade", "downgrade"):
            function = functions[function_name]
            self.assertEqual(len(function.body), 1)
            self.assertIsInstance(function.body[0], ast.Pass)

    def test_revision_graph_has_single_capture_date_expand_head(self) -> None:
        checks = verify_revision_graph(build_alembic_config())

        self.assertIn("single_head=20260901_0002", checks)
        self.assertIn(f"baseline={BASELINE_REVISION}", checks)
        self.assertIn("revision_count=2", checks)

    def test_alembic_config_contains_no_database_url(self) -> None:
        content = (PROJECT_ROOT / "alembic.ini").read_text(encoding="utf-8")

        self.assertNotIn("sqlalchemy.url", content)
        self.assertNotIn("postgresql://", content)

    def test_single_head_validation_fails_closed(self) -> None:
        with self.assertRaises(MigrationVerificationError):
            require_single_head(())
        with self.assertRaises(MigrationVerificationError):
            require_single_head(("head_a", "head_b"))

    def test_session_lock_uses_try_lock_and_explicit_unlock(self) -> None:
        connection = _FakeConnection([True, True])

        with session_advisory_lock(connection):
            pass

        self.assertIn("pg_try_advisory_lock", connection.statements[0][0])
        self.assertIn("pg_advisory_unlock", connection.statements[1][0])
        self.assertEqual(
            connection.statements[0][1]["lock_key"],
            connection.statements[1][1]["lock_key"],
        )
        self.assertEqual(connection.commit_count, 2)

    def test_lock_failure_prevents_migration_operation(self) -> None:
        connection = _FakeConnection([False])
        operation_called = False

        def operation() -> None:
            nonlocal operation_called
            operation_called = True

        with self.assertRaises(MigrationLockUnavailable):
            run_locked_operation(connection, operation)

        self.assertFalse(operation_called)
        self.assertEqual(len(connection.statements), 1)
        self.assertIn("pg_try_advisory_lock", connection.statements[0][0])

    def test_error_masking_removes_plain_encoded_and_url_passwords(self) -> None:
        password = "secret value@123"
        message = (
            "password=plain-secret "
            "postgresql://user:url-secret@db/database "
            f"raw={password} encoded=secret+value%40123"
        )

        masked = mask_secrets(message, (password,))

        self.assertNotIn("plain-secret", masked)
        self.assertNotIn("url-secret", masked)
        self.assertNotIn(password, masked)
        self.assertNotIn("secret+value%40123", masked)
        self.assertGreaterEqual(masked.count("***"), 4)

    def test_bootstrap_objects_are_excluded_from_alembic_filter(self) -> None:
        metadata = MetaData()
        bootstrap_table = Table(
            "bootstrap_only",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("value", String(50)),
        )
        mixed_table = Table(
            "mixed_owner",
            metadata,
            Column("id", Integer, primary_key=True),
            Column(
                "migration_value",
                String(50),
                info=migration_managed_schema_info(),
            ),
        )

        self.assertFalse(
            include_migration_managed_object(
                bootstrap_table,
                bootstrap_table.name,
                "table",
                False,
                None,
            )
        )
        self.assertTrue(
            include_migration_managed_object(
                mixed_table,
                mixed_table.name,
                "table",
                False,
                None,
            )
        )
        self.assertFalse(
            include_migration_managed_object(
                mixed_table.c.id,
                "id",
                "column",
                False,
                None,
            )
        )
        self.assertTrue(
            include_migration_managed_object(
                mixed_table.c.migration_value,
                "migration_value",
                "column",
                False,
                None,
            )
        )
        self.assertFalse(
            include_migration_managed_object(
                object(),
                "legacy_only",
                "column",
                True,
                None,
            )
        )

    def test_ownership_verification_allows_mixed_migration_column(self) -> None:
        metadata = MetaData()
        Table(
            "mixed_migration_column",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("bootstrap_value", String(50)),
            Column(
                "migration_value",
                String(50),
                info=migration_managed_schema_info(),
            ),
        )

        checks = _verify_ownership_projection(
            metadata,
            bootstrap_metadata_projection(metadata),
        )

        self.assertIn("bootstrap_tables=1", checks)
        self.assertIn("migration_scoped_tables=1", checks)

    def test_ownership_verification_allows_mixed_migration_index(self) -> None:
        metadata = MetaData()
        table = Table(
            "mixed_migration_index",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("bootstrap_value", String(50)),
            Column("migration_value", String(50)),
        )
        Index("ix_mixed_bootstrap", table.c.bootstrap_value)
        Index(
            "ix_mixed_migration",
            table.c.migration_value,
            info=migration_managed_schema_info(),
        )

        checks = _verify_ownership_projection(
            metadata,
            bootstrap_metadata_projection(metadata),
        )

        self.assertIn("bootstrap_tables=1", checks)
        self.assertIn("migration_scoped_tables=1", checks)

    def test_ownership_verification_rejects_leaked_migration_column(self) -> None:
        metadata = MetaData()
        Table(
            "leaked_migration_column",
            metadata,
            Column("id", Integer, primary_key=True),
            Column(
                "migration_value",
                String(50),
                info=migration_managed_schema_info(),
            ),
        )
        leaked_projection = MetaData()
        Table(
            "leaked_migration_column",
            leaked_projection,
            Column("id", Integer, primary_key=True),
            Column("migration_value", String(50)),
        )

        with self.assertRaisesRegex(
            MigrationVerificationError,
            r"column:leaked_migration_column\.migration_value",
        ):
            _verify_ownership_projection(metadata, leaked_projection)

    def test_ownership_verification_rejects_leaked_migration_index(self) -> None:
        metadata = MetaData()
        table = Table(
            "leaked_migration_index",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("migration_value", String(50)),
        )
        Index(
            "ix_leaked_migration",
            table.c.migration_value,
            info=migration_managed_schema_info(),
        )

        with self.assertRaisesRegex(
            MigrationVerificationError,
            r"index:leaked_migration_index\.ix_leaked_migration",
        ):
            _verify_ownership_projection(metadata, metadata)

    def test_ownership_verification_rejects_leaked_migration_table(self) -> None:
        metadata = MetaData()
        table = Table(
            "leaked_migration_table",
            metadata,
            Column("id", Integer, primary_key=True),
            info=migration_managed_schema_info(),
        )
        leaked_projection = MetaData()
        table.to_metadata(leaked_projection)

        with self.assertRaisesRegex(
            MigrationVerificationError,
            r"table:leaked_migration_table",
        ):
            _verify_ownership_projection(metadata, leaked_projection)

    def test_api_and_worker_startup_do_not_import_migration_framework(self) -> None:
        startup_files = (
            PROJECT_ROOT / "app" / "main.py",
            PROJECT_ROOT / "app" / "common" / "database.py",
            PROJECT_ROOT / "worker" / "background_worker.py",
            PROJECT_ROOT / "worker" / "vision_worker.py",
            PROJECT_ROOT / "worker" / "plate_solve_worker.py",
        )

        for path in startup_files:
            content = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("scripts.db_migrate", content, path.name)
            self.assertNotIn("alembic", content, path.name)


if __name__ == "__main__":
    unittest.main()
