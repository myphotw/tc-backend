"""Read-only database classification and legacy baseline fingerprint checks.

The fingerprint intentionally covers stable identity and relationship columns,
not every legacy column or index.  This is strict enough to reject a partial or
unrelated schema while permitting harmless extra columns and index-name drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Collection, Sequence

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.reflection import Inspector


PUBLIC_SCHEMA = "public"
ALEMBIC_VERSION_TABLE = "alembic_version"


class DatabaseState(str, Enum):
    EMPTY = "EMPTY"
    LEGACY_UNVERSIONED = "LEGACY_UNVERSIONED"
    VERSIONED = "VERSIONED"
    INVALID_AMBIGUOUS = "INVALID_AMBIGUOUS"


@dataclass(frozen=True)
class ColumnFingerprint:
    name: str
    type_family: str
    nullable: bool
    length: int | None = None
    timezone: bool | None = None


@dataclass(frozen=True)
class ForeignKeyFingerprint:
    columns: tuple[str, ...]
    referred_table: str
    referred_columns: tuple[str, ...]


@dataclass(frozen=True)
class TableFingerprint:
    name: str
    primary_key: tuple[str, ...]
    columns: tuple[ColumnFingerprint, ...]
    foreign_keys: tuple[ForeignKeyFingerprint, ...] = ()
    unique_keys: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class DatabaseAssessment:
    state: DatabaseState
    public_tables: tuple[str, ...]
    current_revisions: tuple[str, ...] = ()
    fingerprint_mismatches: tuple[str, ...] = ()
    state_errors: tuple[str, ...] = ()

    @property
    def baseline_ready(self) -> bool:
        return self.state is DatabaseState.LEGACY_UNVERSIONED


def _column(
    name: str,
    type_family: str,
    nullable: bool,
    *,
    length: int | None = None,
    timezone: bool | None = None,
) -> ColumnFingerprint:
    return ColumnFingerprint(name, type_family, nullable, length, timezone)


def _foreign_key(
    column: str,
    referred_table: str,
    referred_column: str = "id",
) -> ForeignKeyFingerprint:
    return ForeignKeyFingerprint((column,), referred_table, (referred_column,))


# This is an immutable snapshot of the application tables expected at the
# legacy baseline.  It catches a partially initialized schema without making
# every peripheral table's changing columns part of the detailed fingerprint.
BASELINE_REQUIRED_TABLES = frozenset(
    {
        "astro_observation_records",
        "astro_plate_solve_jobs",
        "common_api_keys",
        "common_api_usage",
        "common_change_events",
        "common_file_metadata",
        "common_file_services",
        "common_file_tags",
        "common_files",
        "common_geocode_cache",
        "common_metadata_history",
        "common_settings",
        "common_upload_jobs",
        "common_vision_jobs",
        "common_worker_status",
        "memorykeeper_file_states",
        "memorykeeper_places",
        "mk_file_tag_suppressions",
        "mk_photo_tags",
        "mk_photos",
        "mk_tag_canonical_overrides",
        "mk_tags",
    }
)


# Selection rationale:
# - common_files is the canonical FileAsset identity and storage anchor.
# - metadata/service/upload/change tables prove the current common platform
#   contract rather than merely an old table with the same name.
# - MemoryKeeper file state proves the current service projection and its link
#   to the shared FileAsset.
# - Astro observation and plate-solve tables prove both AstroJournal's domain
#   record and its persistent asynchronous processing contract.
# Extra columns and indexes are deliberately allowed.  They are not stable
# enough to identify the baseline and will evolve through expand migrations.
BASELINE_FINGERPRINT: tuple[TableFingerprint, ...] = (
    TableFingerprint(
        "common_files",
        ("id",),
        (
            _column("id", "integer", False),
            _column("file_id", "string", False, length=64),
            _column("original_name", "string", False, length=255),
            _column("favorite", "boolean", False),
            _column("service_name", "string", False, length=50),
            _column("deleted", "boolean", False),
        ),
        unique_keys=(("file_id",),),
    ),
    TableFingerprint(
        "common_file_metadata",
        ("id",),
        (
            _column("id", "integer", False),
            _column("file_id", "integer", False),
            _column("datetime_original", "datetime", True, timezone=True),
            _column("locked", "boolean", False),
        ),
        (_foreign_key("file_id", "common_files"),),
        (("file_id",),),
    ),
    TableFingerprint(
        "common_file_services",
        ("id",),
        (
            _column("id", "integer", False),
            _column("file_id", "integer", False),
            _column("service_name", "string", False, length=50),
        ),
        (_foreign_key("file_id", "common_files"),),
        (("file_id", "service_name"),),
    ),
    TableFingerprint(
        "common_upload_jobs",
        ("id",),
        (
            _column("id", "integer", False),
            _column("job_id", "string", False, length=36),
            _column("status", "string", False, length=30),
            _column("incoming_path", "text", False),
            _column("service_name", "string", False, length=50),
            _column("client_file_id", "string", True, length=255),
            _column("client_content_sha256", "string", True, length=64),
        ),
    ),
    TableFingerprint(
        "common_settings",
        ("id",),
        (
            _column("id", "integer", False),
            _column("category", "string", False, length=100),
            _column("setting_key", "string", False, length=100),
            _column("setting_value", "string", False, length=500),
        ),
    ),
    TableFingerprint(
        "common_change_events",
        ("id",),
        (
            _column("id", "big_integer", False),
            _column("service_name", "string", False, length=50),
            _column("resource_type", "string", False, length=100),
            _column("resource_id", "string", False, length=255),
            _column("operation", "string", False, length=20),
            _column("tombstone", "boolean", False),
            _column("changed_at", "datetime", False, timezone=True),
        ),
    ),
    TableFingerprint(
        "memorykeeper_file_states",
        ("file_id",),
        (
            _column("file_id", "integer", False),
            _column("favorite", "boolean", False),
            _column("revision", "integer", False),
        ),
        (_foreign_key("file_id", "common_files"),),
    ),
    TableFingerprint(
        "astro_observation_records",
        ("id",),
        (
            _column("id", "string", False, length=36),
            _column("file_id", "integer", False),
            _column("service_name", "string", False, length=50),
            _column("captured_at", "datetime", False, timezone=True),
            _column("plate_solve_status", "string", False, length=30),
            _column("revision", "integer", False),
        ),
        (_foreign_key("file_id", "common_files"),),
    ),
    TableFingerprint(
        "astro_plate_solve_jobs",
        ("id",),
        (
            _column("id", "string", False, length=36),
            _column("common_file_id", "integer", False),
            _column("observation_record_id", "string", True, length=36),
            _column("status", "string", False, length=20),
            _column("attempts", "integer", False),
            _column("ra", "float", True),
            _column("dec", "float", True),
        ),
        (
            _foreign_key("common_file_id", "common_files"),
            _foreign_key("observation_record_id", "astro_observation_records"),
        ),
        (("common_file_id",),),
    ),
)


def application_table_names() -> frozenset[str]:
    """Return model-registered public application table names."""
    from app.common.model_registry import Base

    return frozenset(
        table.name
        for table in Base.metadata.sorted_tables
        if table.schema in (None, PUBLIC_SCHEMA)
    )


def evaluate_baseline_fingerprint(
    inspector: Inspector,
    fingerprint: Sequence[TableFingerprint] = BASELINE_FINGERPRINT,
    *,
    required_tables: Collection[str] = BASELINE_REQUIRED_TABLES,
) -> tuple[str, ...]:
    """Return human-readable mismatches without changing database state."""
    if not fingerprint:
        return ("baseline fingerprint is not configured",)

    public_tables = set(inspector.get_table_names(schema=PUBLIC_SCHEMA))
    mismatches: list[str] = [
        f"missing table public.{table_name}"
        for table_name in sorted(set(required_tables) - public_tables)
    ]

    for table_rule in fingerprint:
        if table_rule.name not in public_tables:
            missing = f"missing table public.{table_rule.name}"
            if missing not in mismatches:
                mismatches.append(missing)
            continue

        actual_columns = {
            column["name"]: column
            for column in inspector.get_columns(
                table_rule.name,
                schema=PUBLIC_SCHEMA,
            )
        }
        for expected in table_rule.columns:
            actual = actual_columns.get(expected.name)
            if actual is None:
                mismatches.append(
                    f"missing column public.{table_rule.name}.{expected.name}"
                )
                continue
            actual_family = _type_family(actual["type"])
            if actual_family != expected.type_family:
                mismatches.append(
                    f"type mismatch public.{table_rule.name}.{expected.name}: "
                    f"expected {expected.type_family}, found {actual_family}"
                )
            if bool(actual.get("nullable")) != expected.nullable:
                mismatches.append(
                    f"nullable mismatch public.{table_rule.name}.{expected.name}: "
                    f"expected {expected.nullable}, found {bool(actual.get('nullable'))}"
                )
            if expected.length is not None:
                actual_length = getattr(actual["type"], "length", None)
                if actual_length != expected.length:
                    mismatches.append(
                        f"length mismatch public.{table_rule.name}.{expected.name}: "
                        f"expected {expected.length}, found {actual_length}"
                    )
            if expected.timezone is not None:
                actual_timezone = bool(getattr(actual["type"], "timezone", False))
                if actual_timezone != expected.timezone:
                    mismatches.append(
                        f"timezone mismatch public.{table_rule.name}.{expected.name}: "
                        f"expected {expected.timezone}, found {actual_timezone}"
                    )

        actual_pk = tuple(
            inspector.get_pk_constraint(
                table_rule.name,
                schema=PUBLIC_SCHEMA,
            ).get("constrained_columns")
            or ()
        )
        if actual_pk != table_rule.primary_key:
            mismatches.append(
                f"primary key mismatch public.{table_rule.name}: "
                f"expected {table_rule.primary_key}, found {actual_pk}"
            )

        actual_foreign_keys = inspector.get_foreign_keys(
            table_rule.name,
            schema=PUBLIC_SCHEMA,
        )
        for expected_fk in table_rule.foreign_keys:
            if not any(
                _foreign_key_matches(actual_fk, expected_fk)
                for actual_fk in actual_foreign_keys
            ):
                mismatches.append(
                    f"foreign key mismatch public.{table_rule.name}"
                    f"({','.join(expected_fk.columns)}) -> "
                    f"public.{expected_fk.referred_table}"
                    f"({','.join(expected_fk.referred_columns)})"
                )

        actual_unique_keys = {
            tuple(constraint.get("column_names") or ())
            for constraint in inspector.get_unique_constraints(
                table_rule.name,
                schema=PUBLIC_SCHEMA,
            )
        }
        actual_unique_keys.update(
            tuple(index.get("column_names") or ())
            for index in inspector.get_indexes(
                table_rule.name,
                schema=PUBLIC_SCHEMA,
            )
            if index.get("unique")
        )
        for expected_unique in table_rule.unique_keys:
            if expected_unique not in actual_unique_keys:
                mismatches.append(
                    f"unique key mismatch public.{table_rule.name}: "
                    f"expected {expected_unique}"
                )

    return tuple(mismatches)


def inspect_database_state(
    connection: Connection,
    *,
    known_revisions: Collection[str],
    inspector: Inspector | None = None,
    known_application_tables: Collection[str] | None = None,
    fingerprint: Sequence[TableFingerprint] = BASELINE_FINGERPRINT,
) -> DatabaseAssessment:
    """Classify the database using read-only catalog and version queries."""
    inspector = inspector or inspect(connection)
    public_tables = set(inspector.get_table_names(schema=PUBLIC_SCHEMA))
    app_tables = set(
        application_table_names()
        if known_application_tables is None
        else known_application_tables
    )
    present_app_tables = public_tables & app_tables
    has_version_table = ALEMBIC_VERSION_TABLE in public_tables

    if not public_tables:
        return DatabaseAssessment(DatabaseState.EMPTY, ())

    if not has_version_table and not present_app_tables:
        return DatabaseAssessment(
            DatabaseState.INVALID_AMBIGUOUS,
            tuple(sorted(public_tables)),
            state_errors=(
                "public schema contains tables but no recognized TC-Backend table",
            ),
        )

    fingerprint_mismatches = evaluate_baseline_fingerprint(inspector, fingerprint)

    if not has_version_table:
        state = (
            DatabaseState.LEGACY_UNVERSIONED
            if not fingerprint_mismatches
            else DatabaseState.INVALID_AMBIGUOUS
        )
        return DatabaseAssessment(
            state,
            tuple(sorted(public_tables)),
            fingerprint_mismatches=fingerprint_mismatches,
        )

    version_errors, current_revisions = _inspect_version_table(
        connection,
        inspector,
        known_revisions,
    )
    state_errors = list(version_errors)
    if not present_app_tables:
        state_errors.append(
            "Alembic version table exists but no TC-Backend application table exists"
        )
    if fingerprint_mismatches:
        state_errors.append("application schema does not match the baseline fingerprint")

    return DatabaseAssessment(
        DatabaseState.INVALID_AMBIGUOUS
        if state_errors
        else DatabaseState.VERSIONED,
        tuple(sorted(public_tables)),
        current_revisions=current_revisions,
        fingerprint_mismatches=fingerprint_mismatches,
        state_errors=tuple(state_errors),
    )


def _inspect_version_table(
    connection: Connection,
    inspector: Inspector,
    known_revisions: Collection[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    errors: list[str] = []
    columns = {
        column["name"]: column
        for column in inspector.get_columns(
            ALEMBIC_VERSION_TABLE,
            schema=PUBLIC_SCHEMA,
        )
    }
    if set(columns) != {"version_num"}:
        errors.append(
            "public.alembic_version must contain only the version_num column"
        )
    version_column = columns.get("version_num")
    if version_column is None:
        errors.append("public.alembic_version.version_num is missing")
    else:
        if _type_family(version_column["type"]) != "string":
            errors.append("public.alembic_version.version_num must be a string")
        if getattr(version_column["type"], "length", None) != 32:
            errors.append("public.alembic_version.version_num must be VARCHAR(32)")
        if bool(version_column.get("nullable")):
            errors.append("public.alembic_version.version_num must be NOT NULL")

    primary_key = tuple(
        inspector.get_pk_constraint(
            ALEMBIC_VERSION_TABLE,
            schema=PUBLIC_SCHEMA,
        ).get("constrained_columns")
        or ()
    )
    if primary_key != ("version_num",):
        errors.append(
            "public.alembic_version primary key must be (version_num)"
        )

    rows = tuple(
        str(value)
        for value in connection.execute(
            text("SELECT version_num FROM public.alembic_version")
        ).scalars().all()
    )
    if len(rows) != 1:
        errors.append(
            "public.alembic_version must contain exactly one revision row; "
            f"found {len(rows)}"
        )
    for revision in rows:
        if revision not in known_revisions:
            errors.append(f"unknown Alembic revision in database: {revision}")

    return tuple(errors), rows


def _foreign_key_matches(
    actual: dict[str, object],
    expected: ForeignKeyFingerprint,
) -> bool:
    referred_schema = actual.get("referred_schema")
    return (
        tuple(actual.get("constrained_columns") or ()) == expected.columns
        and referred_schema in (None, PUBLIC_SCHEMA)
        and actual.get("referred_table") == expected.referred_table
        and tuple(actual.get("referred_columns") or ()) == expected.referred_columns
    )


def _type_family(column_type: object) -> str:
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
