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
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_CONFIG_PATH = PROJECT_ROOT / "alembic.ini"
BASELINE_REVISION = "20260831_0001"

# ASCII-like fixed key: "TCBMIGR1", represented as a signed BIGINT-safe value.
TC_BACKEND_MIGRATION_LOCK_KEY = 0x5443424D49475231

BaselineFingerprintCheck = Callable[[Connection], Sequence[str]]
BASELINE_FINGERPRINT_CHECKS: tuple[BaselineFingerprintCheck, ...] = ()


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
    """Verify the phase-1 baseline and the single-head invariant."""
    scripts = ScriptDirectory.from_config(config)
    head = require_single_head(scripts.get_heads())
    baseline = scripts.get_revision(BASELINE_REVISION)
    if baseline is None:
        raise MigrationVerificationError(
            f"Baseline revision {BASELINE_REVISION} is missing"
        )
    if baseline.down_revision is not None:
        raise MigrationVerificationError("Baseline revision must be the graph root")
    return [
        f"single_head={head}",
        f"baseline={BASELINE_REVISION}",
    ]


def verify_ownership_boundary() -> list[str]:
    """Confirm migration-owned objects cannot enter bootstrap create_all."""
    from app.common.database import Base
    from app.common.schema_sync import (
        bootstrap_managed_tables,
        is_migration_managed,
    )

    bootstrap_tables = set(bootstrap_managed_tables(Base.metadata))
    migration_items = 0
    conflicts: list[str] = []

    for table in Base.metadata.sorted_tables:
        table_migration_owned = is_migration_managed(table)
        child_migration_owned = any(
            is_migration_managed(column) for column in table.columns
        ) or any(is_migration_managed(index) for index in table.indexes)
        if table_migration_owned or child_migration_owned:
            migration_items += 1
            if table in bootstrap_tables:
                conflicts.append(table.name)

    if conflicts:
        raise MigrationVerificationError(
            "Migration-owned schema leaked into bootstrap create_all: "
            + ", ".join(sorted(conflicts))
        )
    return [
        f"bootstrap_tables={len(bootstrap_tables)}",
        f"migration_scoped_tables={migration_items}",
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


def run_baseline_fingerprint_checks(connection: Connection) -> list[str]:
    """Extension point for future production baseline fingerprint checks."""
    results: list[str] = []
    for check in BASELINE_FINGERPRINT_CHECKS:
        results.extend(check(connection))
    if not BASELINE_FINGERPRINT_CHECKS:
        results.append("baseline_fingerprint_checks=not_configured")
    return results


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

    try:
        yield
    finally:
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
    head = script_head(config)
    engine = create_migration_engine()
    try:
        with engine.connect() as connection:
            current = database_current_heads(connection)
    finally:
        engine.dispose()

    rendered = ",".join(current) if current else "UNVERSIONED"
    print(f"script_head={head}")
    print(f"database_heads={rendered}")
    print(f"at_head={current == (head,)}")


def run_upgrade(revision: str) -> None:
    config = build_alembic_config()
    require_single_head(ScriptDirectory.from_config(config).get_heads())
    engine = create_migration_engine()
    try:
        with engine.connect() as connection:
            _attach_connection(config, connection)
            run_locked_operation(
                connection,
                lambda: command.upgrade(config, revision),
            )
    finally:
        engine.dispose()
    print(f"upgrade_complete={revision}")


def run_stamp(revision: str) -> None:
    config = build_alembic_config()
    require_single_head(ScriptDirectory.from_config(config).get_heads())
    engine = create_migration_engine()
    try:
        with engine.connect() as connection:
            _attach_connection(config, connection)
            run_locked_operation(
                connection,
                lambda: command.stamp(config, revision),
            )
    finally:
        engine.dispose()
    print(f"stamp_complete={revision}")


def run_verify() -> None:
    config = build_alembic_config()
    checks = verify_revision_graph(config)
    checks.extend(verify_ownership_boundary())
    expected_head = script_head(config)

    engine = create_migration_engine()
    try:
        with engine.connect() as connection:
            checks.extend(verify_database_revision(connection, expected_head))
            checks.extend(run_baseline_fingerprint_checks(connection))
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

    stamp_parser = subparsers.add_parser("stamp", help="Stamp under advisory lock")
    stamp_parser.add_argument("revision")

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
    elif args.command_name == "stamp":
        run_stamp(args.revision)
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
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
