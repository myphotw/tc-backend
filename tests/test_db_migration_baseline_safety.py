from __future__ import annotations

from contextlib import nullcontext
import unittest
from unittest.mock import patch

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from migrations.baseline import (
    ALEMBIC_VERSION_TABLE,
    BASELINE_FINGERPRINT,
    BASELINE_REQUIRED_TABLES,
    DatabaseAssessment,
    DatabaseState,
    evaluate_baseline_fingerprint,
    inspect_database_state,
)
from scripts.db_migrate import (
    BASELINE_REVISION,
    MigrationVerificationError,
    require_baseline_ready,
    require_versioned_upgrade,
    run_locked_operation,
    run_preflight_baseline,
    run_stamp_baseline,
    run_upgrade,
    verify_revision_graph,
)


class _Rows:
    def __init__(self, values: tuple[str, ...]) -> None:
        self.values = values

    def scalars(self) -> _Rows:
        return self

    def all(self) -> list[str]:
        return list(self.values)


class _VersionConnection:
    def __init__(self, revisions: tuple[str, ...] = ()) -> None:
        self.revisions = revisions
        self.statements: list[str] = []

    def execute(self, statement, parameters=None):
        del parameters
        rendered = str(statement)
        self.statements.append(rendered)
        if "SELECT version_num" not in rendered:
            raise AssertionError(f"unexpected non-read-only SQL: {rendered}")
        return _Rows(self.revisions)


class _ScalarResult:
    def __init__(self, value: bool) -> None:
        self.value = value

    def scalar_one(self) -> bool:
        return self.value


class _LockConnection:
    def __init__(self, results: list[bool]) -> None:
        self.results = list(results)
        self.statements: list[str] = []
        self.transaction_open = False

    def execute(self, statement, parameters=None):
        del parameters
        self.transaction_open = True
        self.statements.append(str(statement))
        return _ScalarResult(self.results.pop(0))

    def in_transaction(self) -> bool:
        return self.transaction_open

    def commit(self) -> None:
        self.transaction_open = False

    def rollback(self) -> None:
        self.transaction_open = False


class _UnlockFailConnection(_LockConnection):
    def execute(self, statement, parameters=None):
        rendered = str(statement)
        if "pg_advisory_unlock" in rendered:
            raise RuntimeError("unlock diagnostic")
        return super().execute(statement, parameters)


class _FakeEngine:
    def __init__(self, connection: _LockConnection) -> None:
        self.connection = connection
        self.disposed = False

    def connect(self):
        return nullcontext(self.connection)

    def dispose(self) -> None:
        self.disposed = True


class _FingerprintInspector:
    def __init__(self) -> None:
        self.tables = set(BASELINE_REQUIRED_TABLES)
        self.columns: dict[str, dict[str, dict[str, object]]] = {}
        self.primary_keys: dict[str, tuple[str, ...]] = {}
        self.foreign_keys: dict[str, list[dict[str, object]]] = {}
        self.unique_keys: dict[str, list[tuple[str, ...]]] = {}

        for table_rule in BASELINE_FINGERPRINT:
            self.columns[table_rule.name] = {
                column.name: {
                    "name": column.name,
                    "type": _sqlalchemy_type(column),
                    "nullable": column.nullable,
                }
                for column in table_rule.columns
            }
            self.primary_keys[table_rule.name] = table_rule.primary_key
            self.foreign_keys[table_rule.name] = [
                {
                    "constrained_columns": list(foreign_key.columns),
                    "referred_schema": "public",
                    "referred_table": foreign_key.referred_table,
                    "referred_columns": list(foreign_key.referred_columns),
                }
                for foreign_key in table_rule.foreign_keys
            ]
            self.unique_keys[table_rule.name] = list(table_rule.unique_keys)

    def add_version_table(self) -> None:
        self.tables.add(ALEMBIC_VERSION_TABLE)
        self.columns[ALEMBIC_VERSION_TABLE] = {
            "version_num": {
                "name": "version_num",
                "type": String(32),
                "nullable": False,
            }
        }
        self.primary_keys[ALEMBIC_VERSION_TABLE] = ("version_num",)

    def get_table_names(self, schema: str) -> list[str]:
        self._require_public(schema)
        return sorted(self.tables)

    def get_columns(self, table_name: str, schema: str) -> list[dict[str, object]]:
        self._require_public(schema)
        return list(self.columns.get(table_name, {}).values())

    def get_pk_constraint(self, table_name: str, schema: str) -> dict[str, object]:
        self._require_public(schema)
        return {"constrained_columns": list(self.primary_keys.get(table_name, ()))}

    def get_foreign_keys(self, table_name: str, schema: str) -> list[dict[str, object]]:
        self._require_public(schema)
        return list(self.foreign_keys.get(table_name, ()))

    def get_unique_constraints(
        self,
        table_name: str,
        schema: str,
    ) -> list[dict[str, object]]:
        self._require_public(schema)
        return [
            {"column_names": list(columns)}
            for columns in self.unique_keys.get(table_name, ())
        ]

    def get_indexes(self, table_name: str, schema: str) -> list[dict[str, object]]:
        self._require_public(schema)
        return []

    @staticmethod
    def _require_public(schema: str) -> None:
        if schema != "public":
            raise AssertionError(f"unexpected schema: {schema}")


def _sqlalchemy_type(column):
    if column.type_family == "integer":
        return Integer()
    if column.type_family == "big_integer":
        return BigInteger()
    if column.type_family == "string":
        return String(column.length)
    if column.type_family == "text":
        return Text()
    if column.type_family == "boolean":
        return Boolean()
    if column.type_family == "datetime":
        return DateTime(timezone=column.timezone)
    if column.type_family == "float":
        return Float()
    raise AssertionError(f"unsupported test type: {column.type_family}")


def _test_type_family(column_type) -> str:
    if isinstance(column_type, BigInteger):
        return "big_integer"
    if isinstance(column_type, Integer):
        return "integer"
    if isinstance(column_type, Text):
        return "text"
    if isinstance(column_type, String):
        return "string"
    if isinstance(column_type, Boolean):
        return "boolean"
    if isinstance(column_type, DateTime):
        return "datetime"
    if isinstance(column_type, Float):
        return "float"
    return type(column_type).__name__.lower()


def _assess(
    inspector: _FingerprintInspector,
    revisions: tuple[str, ...] = (),
) -> tuple[DatabaseAssessment, _VersionConnection]:
    connection = _VersionConnection(revisions)
    assessment = inspect_database_state(
        connection,
        known_revisions={BASELINE_REVISION},
        inspector=inspector,
        known_application_tables=BASELINE_REQUIRED_TABLES,
    )
    return assessment, connection


class DatabaseStateClassificationTests(unittest.TestCase):
    def test_empty_database(self) -> None:
        inspector = _FingerprintInspector()
        inspector.tables.clear()

        assessment, connection = _assess(inspector)

        self.assertIs(assessment.state, DatabaseState.EMPTY)
        self.assertEqual(connection.statements, [])

    def test_valid_legacy_unversioned_database(self) -> None:
        assessment, connection = _assess(_FingerprintInspector())

        self.assertIs(assessment.state, DatabaseState.LEGACY_UNVERSIONED)
        self.assertTrue(assessment.baseline_ready)
        self.assertEqual(assessment.fingerprint_mismatches, ())
        self.assertEqual(connection.statements, [])

    def test_valid_versioned_database(self) -> None:
        inspector = _FingerprintInspector()
        inspector.add_version_table()

        assessment, connection = _assess(inspector, (BASELINE_REVISION,))

        self.assertIs(assessment.state, DatabaseState.VERSIONED)
        self.assertEqual(assessment.current_revisions, (BASELINE_REVISION,))
        self.assertEqual(connection.statements, [
            "SELECT version_num FROM public.alembic_version"
        ])

    def test_partial_schema_is_invalid_ambiguous(self) -> None:
        inspector = _FingerprintInspector()
        inspector.tables.remove("astro_plate_solve_jobs")

        assessment, _ = _assess(inspector)

        self.assertIs(assessment.state, DatabaseState.INVALID_AMBIGUOUS)
        self.assertIn(
            "missing table public.astro_plate_solve_jobs",
            assessment.fingerprint_mismatches,
        )

    def test_unknown_public_schema_is_invalid_ambiguous(self) -> None:
        inspector = _FingerprintInspector()
        inspector.tables = {"unrelated_table"}

        assessment, _ = _assess(inspector)

        self.assertIs(assessment.state, DatabaseState.INVALID_AMBIGUOUS)
        self.assertTrue(assessment.state_errors)

    def test_unknown_alembic_revision_is_invalid(self) -> None:
        inspector = _FingerprintInspector()
        inspector.add_version_table()

        assessment, _ = _assess(inspector, ("unknown_revision",))

        self.assertIs(assessment.state, DatabaseState.INVALID_AMBIGUOUS)
        self.assertIn(
            "unknown Alembic revision in database: unknown_revision",
            assessment.state_errors,
        )

    def test_empty_or_multiple_version_rows_are_invalid(self) -> None:
        for revisions in ((), (BASELINE_REVISION, BASELINE_REVISION)):
            with self.subTest(revisions=revisions):
                inspector = _FingerprintInspector()
                inspector.add_version_table()

                assessment, _ = _assess(inspector, revisions)

                self.assertIs(
                    assessment.state,
                    DatabaseState.INVALID_AMBIGUOUS,
                )
                self.assertTrue(
                    any(
                        "exactly one revision row" in error
                        for error in assessment.state_errors
                    )
                )


class BaselineFingerprintTests(unittest.TestCase):
    def test_fingerprint_snapshot_matches_registered_baseline_models(self) -> None:
        from app.common.model_registry import Base

        model_tables = {
            table.name
            for table in Base.metadata.sorted_tables
            if table.schema in (None, "public")
        }
        self.assertEqual(BASELINE_REQUIRED_TABLES, model_tables)

        for table_rule in BASELINE_FINGERPRINT:
            with self.subTest(table=table_rule.name):
                table = Base.metadata.tables[table_rule.name]
                self.assertEqual(
                    table_rule.primary_key,
                    tuple(column.name for column in table.primary_key.columns),
                )
                for column_rule in table_rule.columns:
                    model_column = table.c[column_rule.name]
                    self.assertEqual(
                        column_rule.type_family,
                        _test_type_family(model_column.type),
                        column_rule.name,
                    )
                    self.assertEqual(
                        column_rule.nullable,
                        model_column.nullable,
                        column_rule.name,
                    )
                    if column_rule.length is not None:
                        self.assertEqual(
                            column_rule.length,
                            getattr(model_column.type, "length", None),
                            column_rule.name,
                        )
                    if column_rule.timezone is not None:
                        self.assertEqual(
                            column_rule.timezone,
                            bool(getattr(model_column.type, "timezone", False)),
                            column_rule.name,
                        )

                model_foreign_keys = {
                    (
                        (foreign_key.parent.name,),
                        foreign_key.column.table.name,
                        (foreign_key.column.name,),
                    )
                    for foreign_key in table.foreign_keys
                }
                for foreign_key in table_rule.foreign_keys:
                    self.assertIn(
                        (
                            foreign_key.columns,
                            foreign_key.referred_table,
                            foreign_key.referred_columns,
                        ),
                        model_foreign_keys,
                    )

                model_unique_keys = {
                    tuple(constraint.columns.keys())
                    for constraint in table.constraints
                    if isinstance(constraint, UniqueConstraint)
                }
                model_unique_keys.update(
                    tuple(index.columns.keys())
                    for index in table.indexes
                    if index.unique
                )
                for unique_key in table_rule.unique_keys:
                    self.assertIn(unique_key, model_unique_keys)

    def test_complete_fingerprint_matches(self) -> None:
        self.assertEqual(
            evaluate_baseline_fingerprint(_FingerprintInspector()),
            (),
        )

    def test_missing_column_is_reported(self) -> None:
        inspector = _FingerprintInspector()
        del inspector.columns["common_files"]["file_id"]

        mismatches = evaluate_baseline_fingerprint(inspector)

        self.assertIn("missing column public.common_files.file_id", mismatches)

    def test_type_mismatch_is_reported(self) -> None:
        inspector = _FingerprintInspector()
        inspector.columns["common_files"]["file_id"]["type"] = Integer()

        mismatches = evaluate_baseline_fingerprint(inspector)

        self.assertTrue(any("type mismatch public.common_files.file_id" in item for item in mismatches))

    def test_nullable_mismatch_is_reported(self) -> None:
        inspector = _FingerprintInspector()
        inspector.columns["common_files"]["file_id"]["nullable"] = True

        mismatches = evaluate_baseline_fingerprint(inspector)

        self.assertTrue(any("nullable mismatch public.common_files.file_id" in item for item in mismatches))

    def test_foreign_key_mismatch_is_reported(self) -> None:
        inspector = _FingerprintInspector()
        inspector.foreign_keys["common_file_services"] = []

        mismatches = evaluate_baseline_fingerprint(inspector)

        self.assertTrue(any("foreign key mismatch public.common_file_services" in item for item in mismatches))

    def test_primary_and_unique_key_mismatches_are_reported(self) -> None:
        inspector = _FingerprintInspector()
        inspector.primary_keys["common_files"] = ()
        inspector.unique_keys["common_files"] = []

        mismatches = evaluate_baseline_fingerprint(inspector)

        self.assertTrue(any("primary key mismatch public.common_files" in item for item in mismatches))
        self.assertTrue(any("unique key mismatch public.common_files" in item for item in mismatches))

    def test_unconfigured_fingerprint_fails_closed(self) -> None:
        mismatches = evaluate_baseline_fingerprint(
            _FingerprintInspector(),
            (),
            required_tables=(),
        )

        self.assertEqual(mismatches, ("baseline fingerprint is not configured",))


class MigrationCommandSafetyTests(unittest.TestCase):
    def test_baseline_accepts_only_valid_legacy_database(self) -> None:
        require_baseline_ready(
            DatabaseAssessment(DatabaseState.LEGACY_UNVERSIONED, ())
        )
        for state in (
            DatabaseState.EMPTY,
            DatabaseState.VERSIONED,
            DatabaseState.INVALID_AMBIGUOUS,
        ):
            with self.subTest(state=state):
                with self.assertRaises(MigrationVerificationError):
                    require_baseline_ready(DatabaseAssessment(state, ()))

    def test_upgrade_accepts_only_versioned_database(self) -> None:
        require_versioned_upgrade(DatabaseAssessment(DatabaseState.VERSIONED, ()))
        for state in (
            DatabaseState.EMPTY,
            DatabaseState.LEGACY_UNVERSIONED,
            DatabaseState.INVALID_AMBIGUOUS,
        ):
            with self.subTest(state=state):
                with self.assertRaises(MigrationVerificationError):
                    require_versioned_upgrade(DatabaseAssessment(state, ()))

    def test_preflight_baseline_uses_only_read_only_assessment(self) -> None:
        connection = _LockConnection([])
        engine = _FakeEngine(connection)
        ready = DatabaseAssessment(DatabaseState.LEGACY_UNVERSIONED, ())

        with (
            patch("scripts.db_migrate.create_migration_engine", return_value=engine),
            patch("scripts.db_migrate.assess_database", return_value=ready) as assess,
            patch("scripts.db_migrate.command.stamp") as stamp,
            patch("scripts.db_migrate.command.upgrade") as upgrade,
        ):
            run_preflight_baseline()

        assess.assert_called_once()
        stamp.assert_not_called()
        upgrade.assert_not_called()
        self.assertEqual(connection.statements, [])

    def test_stamp_baseline_rechecks_fingerprint_inside_lock(self) -> None:
        connection = _LockConnection([True, True])
        engine = _FakeEngine(connection)
        ready = DatabaseAssessment(DatabaseState.LEGACY_UNVERSIONED, ())

        with (
            patch("scripts.db_migrate.create_migration_engine", return_value=engine),
            patch("scripts.db_migrate.assess_database", return_value=ready) as assess,
            patch("scripts.db_migrate.command.stamp") as stamp,
        ):
            run_stamp_baseline()

        assess.assert_called_once()
        stamp.assert_called_once()
        self.assertEqual(stamp.call_args.args[1], BASELINE_REVISION)
        self.assertIn("pg_try_advisory_lock", connection.statements[0])
        self.assertIn("pg_advisory_unlock", connection.statements[-1])

    def test_stamp_baseline_rejects_invalid_state_before_alembic(self) -> None:
        connection = _LockConnection([True, True])
        engine = _FakeEngine(connection)
        invalid = DatabaseAssessment(
            DatabaseState.INVALID_AMBIGUOUS,
            (),
            fingerprint_mismatches=("missing table public.common_files",),
        )

        with (
            patch("scripts.db_migrate.create_migration_engine", return_value=engine),
            patch("scripts.db_migrate.assess_database", return_value=invalid),
            patch("scripts.db_migrate.command.stamp") as stamp,
        ):
            with self.assertRaises(MigrationVerificationError):
                run_stamp_baseline()

        stamp.assert_not_called()

    def test_legacy_unversioned_upgrade_is_rejected_before_alembic(self) -> None:
        connection = _LockConnection([True, True])
        engine = _FakeEngine(connection)
        legacy = DatabaseAssessment(DatabaseState.LEGACY_UNVERSIONED, ())

        with (
            patch("scripts.db_migrate.create_migration_engine", return_value=engine),
            patch("scripts.db_migrate.assess_database", return_value=legacy),
            patch("scripts.db_migrate.command.upgrade") as upgrade,
        ):
            with self.assertRaises(MigrationVerificationError):
                run_upgrade("head")

        upgrade.assert_not_called()

    def test_primary_migration_error_survives_unlock_error(self) -> None:
        connection = _UnlockFailConnection([True])
        primary = ValueError("migration failed")

        def fail() -> None:
            raise primary

        with self.assertRaises(ValueError) as caught:
            run_locked_operation(connection, fail)

        self.assertIs(caught.exception, primary)
        self.assertIsInstance(
            getattr(caught.exception, "migration_unlock_error", None),
            RuntimeError,
        )

    def test_separate_revision_root_is_rejected(self) -> None:
        class _Graph:
            def get_heads(self):
                return ("merged_head",)

            def get_bases(self):
                return (BASELINE_REVISION, "foreign_root")

        with patch(
            "scripts.db_migrate.ScriptDirectory.from_config",
            return_value=_Graph(),
        ):
            with self.assertRaises(MigrationVerificationError):
                verify_revision_graph(object())


if __name__ == "__main__":
    unittest.main()
