"""Operator-only Alembic CLI for TC-Backend.

This module is intentionally not imported by API or worker startup code.
Online Alembic commands share one connection with the PostgreSQL session-level
advisory lock so that the lock survives per-revision transaction boundaries.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import quote_plus

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool

from migrations.baseline import (
    DatabaseAssessment,
    DatabaseState,
    inspect_database_state,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_CONFIG_PATH = PROJECT_ROOT / "alembic.ini"
BASELINE_REVISION = "20260831_0001"

# ASCII-like fixed key: "TCBMIGR1", represented as a signed BIGINT-safe value.
TC_BACKEND_MIGRATION_LOCK_KEY = 0x5443424D49475231

class MigrationCliError(RuntimeError):
    """Base error for safe CLI failures."""


class MigrationLockUnavailable(MigrationCliError):
    """Raised when another migration session owns the advisory lock."""


class MigrationVerificationError(MigrationCliError):
    """Raised when framework or database verification fails."""


def build_alembic_config() -> Config:
    """Build a secret-free Alembic configuration."""
    if not ALEMBIC_CONFIG_PATH.is_file():
        raise MigrationVerificationError(
            f"Alembic config not found: {ALEMBIC_CONFIG_PATH.name}"
        )
    return Config(str(ALEMBIC_CONFIG_PATH))


def create_migration_engine() -> Engine:
    """Create the short-lived PostgreSQL engine used only by this CLI."""
    from app.common.database import DATABASE_URL

    return create_engine(
        DATABASE_URL,
        poolclass=NullPool,
        connect_args={"options": "-csearch_path=public"},
    )


def require_single_head(heads: Sequence[str]) -> str:
    """Return the only revision head or fail closed."""
    normalized = tuple(heads)
    if len(normalized) != 1:
        raise MigrationVerificationError(
            f"Expected exactly one Alembic head, found {len(normalized)}"
        )
    return normalized[0]


def script_head(config: Config) -> str:
    scripts = ScriptDirectory.from_config(config)
    return require_single_head(scripts.get_heads())


def verify_revision_graph(config: Config) -> list[str]:
    """Verify one baseline root and a single lineage ending at one head."""
    scripts = ScriptDirectory.from_config(config)
    head = require_single_head(scripts.get_heads())
    bases = tuple(scripts.get_bases())
    if bases != (BASELINE_REVISION,):
        rendered = ",".join(bases) if bases else "NONE"
        raise MigrationVerificationError(
            f"Expected baseline as the only revision root; found {rendered}"
        )

    baseline = scripts.get_revision(BASELINE_REVISION)
    if baseline is None:
        raise MigrationVerificationError(
            f"Baseline revision {BASELINE_REVISION} is missing"
        )
    if baseline.down_revision is not None:
        raise MigrationVerificationError("Baseline revision must be the graph root")

    revisions = {revision.revision: revision for revision in scripts.walk_revisions()}
    if BASELINE_REVISION not in revisions:
        raise MigrationVerificationError("Baseline revision is outside the revision graph")
    for revision_id in revisions:
        if revision_id == BASELINE_REVISION:
            continue
        if not _revision_descends_from_baseline(revision_id, revisions):
            raise MigrationVerificationError(
                f"Revision {revision_id} is not descended from {BASELINE_REVISION}"
            )
    if not _revision_descends_from_baseline(head, revisions, allow_self=True):
        raise MigrationVerificationError(
            f"Head {head} is not in the baseline revision lineage"
        )

    return [
        f"single_head={head}",
        f"baseline={BASELINE_REVISION}",
        f"revision_count={len(revisions)}",
    ]


def _revision_descends_from_baseline(
    revision_id: str,
    revisions: dict[str, Any],
    *,
    allow_self: bool = False,
) -> bool:
    if allow_self and revision_id == BASELINE_REVISION:
        return True

    pending = [revision_id]
    visited: set[str] = set()
    while pending:
        current_id = pending.pop()
        if current_id in visited:
            continue
        visited.add(current_id)
        current = revisions.get(current_id)
        if current is None:
            return False
        parents = current.down_revision
        if parents is None:
            continue
        parent_ids = (parents,) if isinstance(parents, str) else tuple(parents)
        if BASELINE_REVISION in parent_ids:
            return True
        pending.extend(parent_ids)
    return False


def known_script_revisions(config: Config) -> frozenset[str]:
    scripts = ScriptDirectory.from_config(config)
    return frozenset(revision.revision for revision in scripts.walk_revisions())


def verify_ownership_boundary() -> list[str]:
    """Confirm the bootstrap DDL projection excludes migration-owned schema."""
    from app.common.model_registry import Base
    from app.common.schema_sync import bootstrap_metadata_projection

    return _verify_ownership_projection(
        Base.metadata,
        bootstrap_metadata_projection(Base.metadata),
    )


def _verify_ownership_projection(
    metadata: MetaData,
    projection: MetaData,
) -> list[str]:
    """Validate source ownership markers against startup DDL projection.

    ``bootstrap_metadata_projection`` keeps a mixed-ownership table in the
    projection but marks copied migration-owned columns as ``system`` so that
    SQLAlchemy omits them from ``CREATE TABLE``.  Verification must therefore
    inspect that rendered-DDL marker rather than treating a mixed table itself
    as a leak.
    """
    from app.common.schema_sync import (
        bootstrap_managed_tables,
        is_migration_managed,
        table_has_migration_managed_schema,
    )

    bootstrap_tables = bootstrap_managed_tables(metadata)
    migration_scoped_tables = sum(
        table_has_migration_managed_schema(table)
        for table in metadata.sorted_tables
    )

    conflicts: list[str] = []

    for table in metadata.sorted_tables:
        projected_table = projection.tables.get(table.key)
        if is_migration_managed(table):
            if projected_table is not None:
                conflicts.append(f"table:{table.fullname}")
            continue

        if projected_table is None:
            conflicts.append(f"table:{table.fullname}")
            continue

        for column in table.columns:
            projected_column = projected_table.c.get(column.name)
            if is_migration_managed(column):
                if projected_column is None or not projected_column.system:
                    conflicts.append(f"column:{table.fullname}.{column.name}")
            elif projected_column is None or projected_column.system:
                conflicts.append(f"column:{table.fullname}.{column.name}")

        projected_index_names = {
            index.name
            for index in projected_table.indexes
            if index.name is not None
        }
        for index in table.indexes:
            if index.name is None:
                continue
            if is_migration_managed(index):
                if index.name in projected_index_names:
                    conflicts.append(f"index:{table.fullname}.{index.name}")
            elif index.name not in projected_index_names:
                conflicts.append(f"index:{table.fullname}.{index.name}")

    if conflicts:
        raise MigrationVerificationError(
            "Schema ownership conflicts in bootstrap projection: "
            + ", ".join(sorted(conflicts))
        )
    return [
        f"bootstrap_tables={len(bootstrap_tables)}",
        f"migration_scoped_tables={migration_scoped_tables}",
    ]


def database_current_heads(connection: Connection) -> tuple[str, ...]:
    """Read applied Alembic heads without changing application schema."""
    migration_context = MigrationContext.configure(
        connection,
        opts={
            "version_table": "alembic_version",
            "version_table_schema": "public",
        },
    )
    return tuple(migration_context.get_current_heads())


def verify_database_revision(connection: Connection, expected_head: str) -> list[str]:
    current = database_current_heads(connection)
    if current != (expected_head,):
        rendered = ",".join(current) if current else "UNVERSIONED"
        raise MigrationVerificationError(
            f"Database revision is {rendered}; expected {expected_head}"
        )
    return [f"database_head={expected_head}"]


def assess_database(config: Config, connection: Connection) -> DatabaseAssessment:
    """Run the shared read-only state and fingerprint assessment."""
    return inspect_database_state(
        connection,
        known_revisions=known_script_revisions(config),
    )


def require_baseline_ready(assessment: DatabaseAssessment) -> None:
    if assessment.state is DatabaseState.LEGACY_UNVERSIONED:
        return
    if assessment.state is DatabaseState.EMPTY:
        detail = "database is empty; use the future bootstrap-empty path"
    elif assessment.state is DatabaseState.VERSIONED:
        detail = "database is already versioned and is not a baseline candidate"
    else:
        detail = "database state or baseline fingerprint is invalid/ambiguous"
    diagnostics = (
        *assessment.state_errors,
        *assessment.fingerprint_mismatches,
    )
    if diagnostics:
        detail += "; " + "; ".join(diagnostics)
    raise MigrationVerificationError(f"Baseline refused: {detail}")


def require_versioned_upgrade(assessment: DatabaseAssessment) -> None:
    if assessment.state is DatabaseState.VERSIONED:
        return
    if assessment.state is DatabaseState.LEGACY_UNVERSIONED:
        detail = "run preflight-baseline and stamp-baseline before upgrade"
    elif assessment.state is DatabaseState.EMPTY:
        detail = "use the future bootstrap-empty path before upgrade"
    else:
        detail = "database state is invalid/ambiguous"
    if assessment.state_errors:
        detail += "; " + "; ".join(assessment.state_errors)
    raise MigrationVerificationError(f"Upgrade refused: {detail}")


def print_database_assessment(assessment: DatabaseAssessment) -> None:
    print(f"database_state={assessment.state.value}")
    revisions = ",".join(assessment.current_revisions) or "UNVERSIONED"
    print(f"database_revisions={revisions}")
    if (
        assessment.state
        in {DatabaseState.LEGACY_UNVERSIONED, DatabaseState.VERSIONED}
        and not assessment.fingerprint_mismatches
    ):
        print("baseline_fingerprint=MATCH")
    elif assessment.state is DatabaseState.EMPTY:
        print("baseline_fingerprint=NOT_APPLICABLE")
    for mismatch in assessment.fingerprint_mismatches:
        print(f"fingerprint_mismatch={mismatch}")
    for error in assessment.state_errors:
        print(f"state_error={error}")


def _commit_open_transaction(connection: Connection) -> None:
    if connection.in_transaction():
        connection.commit()


def _rollback_open_transaction(connection: Connection) -> None:
    if connection.in_transaction():
        connection.rollback()


@contextmanager
def session_advisory_lock(connection: Connection) -> Iterator[None]:
    """Hold the TC-Backend migration lock for the connection's full session."""
    acquired = bool(
        connection.execute(
            text("SELECT pg_try_advisory_lock(:lock_key)"),
            {"lock_key": TC_BACKEND_MIGRATION_LOCK_KEY},
        ).scalar_one()
    )
    # The lock is session-scoped, so committing this short transaction keeps it.
    _commit_open_transaction(connection)
    if not acquired:
        raise MigrationLockUnavailable(
            "Another TC-Backend migration session already holds the advisory lock"
        )

    primary_error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            # Alembic may leave a failed transaction open. End it before unlock.
            _rollback_open_transaction(connection)
            released = bool(
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": TC_BACKEND_MIGRATION_LOCK_KEY},
                ).scalar_one()
            )
            _commit_open_transaction(connection)
            if not released:
                raise MigrationCliError(
                    "TC-Backend migration advisory lock was not owned during unlock"
                )
        except BaseException as unlock_error:
            if primary_error is None:
                raise
            # Keep the migration/stamp exception and its traceback primary.  The
            # CLI prints this attached failure as a separate masked diagnostic.
            setattr(primary_error, "migration_unlock_error", unlock_error)


def run_locked_operation(connection: Connection, operation: Callable[[], Any]) -> Any:
    """Run a mutating operation only after acquiring the session lock."""
    with session_advisory_lock(connection):
        return operation()


def _attach_connection(config: Config, connection: Connection) -> None:
    config.attributes["connection"] = connection


def run_current(verbose: bool) -> None:
    config = build_alembic_config()
    engine = create_migration_engine()
    try:
        with engine.connect() as connection:
            _attach_connection(config, connection)
            command.current(config, verbose=verbose)
    finally:
        engine.dispose()


def run_status() -> None:
    config = build_alembic_config()
    verify_revision_graph(config)
    head = script_head(config)
    engine = create_migration_engine()
    try:
        with engine.connect() as connection:
            assessment = assess_database(config, connection)
    finally:
        engine.dispose()

    print_database_assessment(assessment)
    current = assessment.current_revisions
    print(f"script_head={head}")
    print(f"at_head={current == (head,)}")


def run_upgrade(revision: str) -> None:
    config = build_alembic_config()
    verify_revision_graph(config)
    engine = create_migration_engine()
    try:
        with engine.connect() as connection:
            _attach_connection(config, connection)

            def guarded_upgrade() -> None:
                assessment = assess_database(config, connection)
                require_versioned_upgrade(assessment)
                # Inspector/catalog reads use SQLAlchemy autobegin. End that
                # read transaction before Alembic opens revision transactions;
                # the PostgreSQL session-level advisory lock remains held.
                _rollback_open_transaction(connection)
                command.upgrade(config, revision)

            run_locked_operation(
                connection,
                guarded_upgrade,
            )
    finally:
        engine.dispose()
    print(f"upgrade_complete={revision}")


def run_preflight_baseline() -> None:
    config = build_alembic_config()
    verify_revision_graph(config)
    engine = create_migration_engine()
    try:
        with engine.connect() as connection:
            assessment = assess_database(config, connection)
    finally:
        engine.dispose()
    print_database_assessment(assessment)
    require_baseline_ready(assessment)
    print("baseline_preflight=BASELINE_READY")


def run_stamp_baseline() -> None:
    """Stamp only the reviewed legacy baseline; generic stamp is unsupported."""
    config = build_alembic_config()
    verify_revision_graph(config)
    engine = create_migration_engine()
    try:
        with engine.connect() as connection:
            _attach_connection(config, connection)

            def guarded_stamp() -> None:
                # Re-run the complete read-only assessment while holding the
                # migration lock.  A prior preflight result is never trusted.
                assessment = assess_database(config, connection)
                require_baseline_ready(assessment)
                _rollback_open_transaction(connection)
                command.stamp(config, BASELINE_REVISION)

            run_locked_operation(connection, guarded_stamp)
    finally:
        engine.dispose()
    print(f"stamp_complete={BASELINE_REVISION}")


def run_verify() -> None:
    config = build_alembic_config()
    checks = verify_revision_graph(config)
    checks.extend(verify_ownership_boundary())
    expected_head = script_head(config)

    engine = create_migration_engine()
    try:
        with engine.connect() as connection:
            assessment = assess_database(config, connection)
            if assessment.state is not DatabaseState.VERSIONED:
                raise MigrationVerificationError(
                    f"Database state is {assessment.state.value}; expected VERSIONED"
                )
            checks.extend(verify_database_revision(connection, expected_head))
            checks.append("baseline_fingerprint=MATCH")
    finally:
        engine.dispose()

    for result in checks:
        print(f"ok {result}")


_POSTGRES_URL_PASSWORD = re.compile(
    r"(?i)(postgres(?:ql)?(?:\+[a-z0-9_]+)?://[^:\s/@]+:)([^@\s]+)(@)"
)
_PASSWORD_ASSIGNMENT = re.compile(r"(?i)(password\s*[=:]\s*)([^\s,;]+)")


def mask_secrets(message: object, secrets: Sequence[str] = ()) -> str:
    """Mask configured and URL-shaped credentials in operator-facing errors."""
    masked = str(message)
    for secret in secrets:
        if not secret:
            continue
        masked = masked.replace(secret, "***")
        masked = masked.replace(quote_plus(secret), "***")
    masked = _POSTGRES_URL_PASSWORD.sub(r"\1***\3", masked)
    masked = _PASSWORD_ASSIGNMENT.sub(r"\1***", masked)
    return masked


def configured_secrets() -> tuple[str, ...]:
    try:
        from app.common.config import settings
    except Exception:
        return ()
    return (settings.POSTGRES_PASSWORD,)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Explicit TC-Backend database migration commands"
    )
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    subparsers.add_parser("status", help="Show script and database revision status")

    current_parser = subparsers.add_parser("current", help="Show database revision")
    current_parser.add_argument("--verbose", action="store_true")

    heads_parser = subparsers.add_parser("heads", help="Show migration graph head")
    heads_parser.add_argument("--verbose", action="store_true")

    history_parser = subparsers.add_parser("history", help="Show revision history")
    history_parser.add_argument("--verbose", action="store_true")

    upgrade_parser = subparsers.add_parser("upgrade", help="Upgrade under advisory lock")
    upgrade_parser.add_argument("revision", nargs="?", default="head")

    subparsers.add_parser(
        "preflight-baseline",
        help="Read-only legacy production baseline fingerprint check",
    )
    subparsers.add_parser(
        "stamp-baseline",
        help="Revalidate and stamp only the fixed legacy baseline",
    )

    subparsers.add_parser("verify", help="Verify graph, ownership, and database revision")
    return parser


def dispatch(args: argparse.Namespace) -> None:
    if args.command_name == "status":
        run_status()
    elif args.command_name == "current":
        run_current(args.verbose)
    elif args.command_name == "heads":
        command.heads(build_alembic_config(), verbose=args.verbose)
    elif args.command_name == "history":
        command.history(build_alembic_config(), verbose=args.verbose)
    elif args.command_name == "upgrade":
        run_upgrade(args.revision)
    elif args.command_name == "preflight-baseline":
        run_preflight_baseline()
    elif args.command_name == "stamp-baseline":
        run_stamp_baseline()
    elif args.command_name == "verify":
        run_verify()
    else:  # pragma: no cover - argparse enforces the command choices.
        raise MigrationCliError(f"Unsupported command: {args.command_name}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        dispatch(args)
    except Exception as exc:
        safe_message = mask_secrets(exc, configured_secrets())
        print(
            f"migration command failed ({type(exc).__name__}): {safe_message}",
            file=sys.stderr,
        )
        unlock_error = getattr(exc, "migration_unlock_error", None)
        if unlock_error is not None:
            safe_unlock_message = mask_secrets(
                unlock_error,
                configured_secrets(),
            )
            print(
                "secondary advisory unlock failure "
                f"({type(unlock_error).__name__}): {safe_unlock_message}",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
