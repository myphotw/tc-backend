from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from migrations.baseline import DatabaseState
from scripts.db_migrate import (
    BASELINE_REVISION,
    MigrationVerificationError,
    assess_database,
    build_alembic_config,
    run_current,
    run_preflight_baseline,
    run_stamp_baseline,
    run_status,
    run_upgrade,
    run_verify,
)
from tests.integration.postgresql.support import (
    application_schema_snapshot,
    create_legacy_schema,
    public_catalog_snapshot,
    run_with_engine_patch,
    table_exists,
)


pytestmark = pytest.mark.postgresql_integration


@pytest.mark.parametrize(
    ("command_name", "operation", "raises"),
    (
        ("status", run_status, False),
        ("current", lambda: run_current(False), False),
        ("verify", run_verify, True),
        ("preflight-baseline", run_preflight_baseline, True),
    ),
)
def test_read_only_commands_do_not_create_version_or_public_objects(
    command_name: str,
    operation,
    raises: bool,
    postgresql_engine: Engine,
    migration_engine_factory,
    capsys,
) -> None:
    del command_name
    with postgresql_engine.connect() as connection:
        before = public_catalog_snapshot(connection)
        connection.rollback()

    if raises:
        with pytest.raises(MigrationVerificationError):
            run_with_engine_patch(migration_engine_factory, operation)
    else:
        run_with_engine_patch(migration_engine_factory, operation)
    capsys.readouterr()

    with postgresql_engine.connect() as connection:
        after = public_catalog_snapshot(connection)
        assert not table_exists(connection, "alembic_version")
    assert after == before


def test_valid_legacy_preflight_stamp_and_verify_preserve_application_schema(
    postgresql_engine: Engine,
    migration_engine_factory,
    capsys,
) -> None:
    create_legacy_schema(postgresql_engine)
    with postgresql_engine.connect() as connection:
        before = application_schema_snapshot(connection)
        assessment = assess_database(build_alembic_config(), connection)
        assert assessment.state is DatabaseState.LEGACY_UNVERSIONED
        connection.rollback()

    run_with_engine_patch(migration_engine_factory, run_preflight_baseline)
    preflight_output = capsys.readouterr().out
    assert "baseline_preflight=BASELINE_READY" in preflight_output
    assert "baseline_fingerprint=MATCH" in preflight_output

    with postgresql_engine.connect() as connection:
        assert application_schema_snapshot(connection) == before
        assert not table_exists(connection, "alembic_version")

    run_with_engine_patch(migration_engine_factory, run_stamp_baseline)
    capsys.readouterr()

    with postgresql_engine.connect() as connection:
        assert table_exists(connection, "alembic_version")
        revision = connection.execute(
            text("SELECT version_num FROM public.alembic_version")
        ).scalar_one()
        assert revision == BASELINE_REVISION
        assert application_schema_snapshot(connection) == before
        assessment = assess_database(build_alembic_config(), connection)
        assert assessment.state is DatabaseState.VERSIONED
        connection.rollback()

    run_with_engine_patch(migration_engine_factory, run_verify)
    verify_output = capsys.readouterr().out
    assert f"ok database_head={BASELINE_REVISION}" in verify_output


def _prepare_state(engine: Engine, state_name: str) -> None:
    if state_name == "empty":
        return
    if state_name == "partial":
        from app.common.models.file import CommonFile

        CommonFile.__table__.create(bind=engine)
        return
    if state_name == "fingerprint_mismatch":
        create_legacy_schema(engine)
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE common_files ALTER COLUMN file_id DROP NOT NULL")
            )
        return
    if state_name == "legacy":
        create_legacy_schema(engine)
        return
    raise AssertionError(f"unsupported test state: {state_name}")


@pytest.mark.parametrize("state_name", ("empty", "partial", "fingerprint_mismatch"))
def test_baseline_stamp_rejects_nonmatching_database_without_side_effects(
    state_name: str,
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    _prepare_state(postgresql_engine, state_name)
    with postgresql_engine.connect() as connection:
        before = public_catalog_snapshot(connection)
        connection.rollback()

    with pytest.raises(MigrationVerificationError):
        run_with_engine_patch(migration_engine_factory, run_stamp_baseline)

    with postgresql_engine.connect() as connection:
        after = public_catalog_snapshot(connection)
        assert not table_exists(connection, "alembic_version")
    assert after == before


def test_preflight_reports_human_readable_fingerprint_mismatch(
    postgresql_engine: Engine,
    migration_engine_factory,
    capsys,
) -> None:
    _prepare_state(postgresql_engine, "partial")

    with pytest.raises(MigrationVerificationError):
        run_with_engine_patch(migration_engine_factory, run_preflight_baseline)

    output = capsys.readouterr().out
    assert "database_state=INVALID_AMBIGUOUS" in output
    assert "fingerprint_mismatch=missing table" in output


@pytest.mark.parametrize("state_name", ("empty", "legacy", "partial"))
def test_unversioned_upgrade_is_rejected_without_creating_version_table(
    state_name: str,
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    _prepare_state(postgresql_engine, state_name)
    with postgresql_engine.connect() as connection:
        before = public_catalog_snapshot(connection)
        connection.rollback()

    with pytest.raises(MigrationVerificationError):
        run_with_engine_patch(
            migration_engine_factory,
            lambda: run_upgrade("head"),
        )

    with postgresql_engine.connect() as connection:
        after = public_catalog_snapshot(connection)
        assert not table_exists(connection, "alembic_version")
    assert after == before


def _create_malformed_version_table(engine: Engine, case: str) -> None:
    with engine.begin() as connection:
        if case == "wrong_structure":
            connection.execute(
                text(
                    "CREATE TABLE public.alembic_version "
                    "(version_num INTEGER PRIMARY KEY)"
                )
            )
            connection.execute(
                text("INSERT INTO public.alembic_version VALUES (123)")
            )
            return

        connection.execute(
            text(
                "CREATE TABLE public.alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
        )
        if case == "unknown":
            connection.execute(
                text("INSERT INTO public.alembic_version VALUES ('unknown_revision')")
            )
        elif case == "multiple":
            connection.execute(
                text(
                    "INSERT INTO public.alembic_version VALUES "
                    "('20260831_0001'), ('unknown_revision')"
                )
            )
        elif case != "zero":
            raise AssertionError(f"unsupported malformed case: {case}")


@pytest.mark.parametrize("case", ("zero", "multiple", "unknown", "wrong_structure"))
def test_malformed_version_table_blocks_stamp_and_upgrade(
    case: str,
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    create_legacy_schema(postgresql_engine)
    _create_malformed_version_table(postgresql_engine, case)
    with postgresql_engine.connect() as connection:
        before = public_catalog_snapshot(connection)
        assessment = assess_database(build_alembic_config(), connection)
        assert assessment.state is DatabaseState.INVALID_AMBIGUOUS
        connection.rollback()

    with pytest.raises(MigrationVerificationError):
        run_with_engine_patch(migration_engine_factory, run_stamp_baseline)
    with pytest.raises(MigrationVerificationError):
        run_with_engine_patch(
            migration_engine_factory,
            lambda: run_upgrade("head"),
        )

    with postgresql_engine.connect() as connection:
        after = public_catalog_snapshot(connection)
    assert after == before
