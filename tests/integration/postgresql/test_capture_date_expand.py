from __future__ import annotations

from datetime import date, datetime

from alembic import command
import pytest
from sqlalchemy import Date, DateTime, Integer, String, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.memorykeeper.models.file_state import MemoryKeeperFileState
from scripts.db_migrate import (
    BASELINE_REVISION,
    build_alembic_config,
    run_stamp_baseline,
    run_upgrade,
    verify_ownership_boundary,
)
from tests.integration.postgresql.support import (
    create_legacy_schema,
    run_with_engine_patch,
)


pytestmark = pytest.mark.postgresql_integration

HEAD_REVISION = "20260925_0008"


def test_capture_date_expand_upgrade_generated_values_and_downgrade(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    # The isolated public schema starts empty.  Create the baseline-era shape,
    # stamp it, and prove Alembic alone performs this nullable expand.
    create_legacy_schema(postgresql_engine)
    run_with_engine_patch(migration_engine_factory, run_stamp_baseline)
    run_with_engine_patch(migration_engine_factory, lambda: run_upgrade("head"))

    reflected = inspect(postgresql_engine)
    metadata_columns = {
        column["name"]: column
        for column in reflected.get_columns("common_file_metadata", schema="public")
    }
    state_columns = {
        column["name"]: column
        for column in reflected.get_columns(
            "memorykeeper_file_states",
            schema="public",
        )
    }

    original = metadata_columns["original_capture_datetime"]
    assert isinstance(original["type"], DateTime)
    assert original["type"].timezone is False
    assert original["nullable"] is True

    for name, column_type in {
        "user_capture_datetime": DateTime,
        "user_capture_precision": String,
        "source_capture_year": Integer,
        "source_capture_year_basis": String,
        "effective_capture_datetime": DateTime,
        "effective_capture_date": Date,
        "effective_capture_year": Integer,
        "effective_capture_year_v2": Integer,
        "effective_capture_precision": String,
        "date_basis": String,
    }.items():
        column = state_columns[name]
        assert column["nullable"] is True
        assert isinstance(column["type"], column_type)
        if column_type is String and name != "source_capture_year_basis":
            assert getattr(column["type"], "length", None) == 16

    assert state_columns["source_capture_year_basis"]["type"].length == 32

    assert state_columns["user_capture_datetime"]["type"].timezone is False
    assert state_columns["effective_capture_datetime"]["type"].timezone is False
    for name in ("effective_capture_date", "effective_capture_year"):
        computed = state_columns[name].get("computed")
        assert computed is not None
        assert computed.get("persisted") is True
        assert "effective_capture_datetime" in computed.get("sqltext", "")
    assert state_columns["effective_capture_year_v2"].get("computed") is None

    with postgresql_engine.begin() as connection:
        common_file_id = connection.execute(
            text(
                """
                INSERT INTO common_files (file_id, original_name, deleted)
                VALUES (:file_id, :original_name, false)
                RETURNING id
                """
            ),
            {
                "file_id": "c" * 64,
                "original_name": "capture-date.jpg",
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO memorykeeper_file_states (
                    file_id,
                    effective_capture_datetime,
                    effective_capture_year_v2,
                    effective_capture_precision
                ) VALUES (:file_id, :captured_at, 2024, 'DATETIME')
                """
            ),
            {
                "file_id": common_file_id,
                "captured_at": datetime(2024, 2, 29, 23, 45, 12),
            },
        )

    with postgresql_engine.connect() as connection:
        derived = connection.execute(
            text(
                """
                SELECT effective_capture_date,
                       effective_capture_year,
                       effective_capture_year_v2
                FROM memorykeeper_file_states
                WHERE file_id = :file_id
                """
            ),
            {"file_id": common_file_id},
        ).one()
        assert derived == (date(2024, 2, 29), 2024, 2024)
        revision = connection.execute(
            text("SELECT version_num FROM public.alembic_version")
        ).scalar_one()
        assert revision == HEAD_REVISION
        connection.rollback()

    with Session(postgresql_engine) as session:
        state = session.get(MemoryKeeperFileState, common_file_id)
        assert state is not None
        assert state.effective_capture_datetime == datetime(2024, 2, 29, 23, 45, 12)
        assert state.effective_capture_date == date(2024, 2, 29)
        assert state.effective_capture_year == 2024

    with postgresql_engine.begin() as connection:
        year_only_file_id = connection.execute(
            text(
                """
                INSERT INTO common_files (file_id, original_name, deleted)
                VALUES (:file_id, :original_name, false)
                RETURNING id
                """
            ),
            {"file_id": "d" * 64, "original_name": "year-only.jpg"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO memorykeeper_file_states (
                    file_id,
                    source_capture_year,
                    source_capture_year_basis,
                    effective_capture_year_v2,
                    effective_capture_precision,
                    date_basis
                ) VALUES (:file_id, 2023, 'ORIGINAL_PATH', 2023, 'YEAR', 'SOURCE_YEAR')
                """
            ),
            {"file_id": year_only_file_id},
        )
    with postgresql_engine.connect() as connection:
        year_only = connection.execute(
            text(
                """
                SELECT effective_capture_datetime,
                       effective_capture_date,
                       effective_capture_year,
                       effective_capture_year_v2
                FROM memorykeeper_file_states
                WHERE file_id = :file_id
                """
            ),
            {"file_id": year_only_file_id},
        ).one()
        assert year_only == (None, None, None, 2023)
        connection.rollback()

    # Use the same guarded disposable connection supplied by the integration
    # fixture; the application runner intentionally exposes no downgrade CLI.
    config = build_alembic_config()
    with postgresql_engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, BASELINE_REVISION)
        connection.commit()

    downgraded = inspect(postgresql_engine)
    after_metadata = {
        column["name"]
        for column in downgraded.get_columns("common_file_metadata", schema="public")
    }
    after_state = {
        column["name"]
        for column in downgraded.get_columns(
            "memorykeeper_file_states",
            schema="public",
        )
    }
    assert "original_capture_datetime" not in after_metadata
    assert {
        "user_capture_datetime",
        "user_capture_precision",
        "source_capture_year",
        "source_capture_year_basis",
        "effective_capture_datetime",
        "effective_capture_date",
        "effective_capture_year",
        "effective_capture_year_v2",
        "effective_capture_precision",
        "date_basis",
    }.isdisjoint(after_state)


def test_capture_date_model_ownership_is_verifiable() -> None:
    checks = verify_ownership_boundary()
    assert any(check.startswith("migration_scoped_tables=") for check in checks)


def test_year_only_expand_preserves_existing_exact_projection(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    create_legacy_schema(postgresql_engine)
    run_with_engine_patch(migration_engine_factory, run_stamp_baseline)
    run_with_engine_patch(
        migration_engine_factory,
        lambda: run_upgrade("20260914_0007"),
    )

    captured_at = datetime(2022, 7, 8, 9, 10, 11)
    with postgresql_engine.begin() as connection:
        common_file_id = connection.execute(
            text(
                """
                INSERT INTO common_files (file_id, original_name, deleted)
                VALUES (:file_id, :original_name, false)
                RETURNING id
                """
            ),
            {"file_id": "e" * 64, "original_name": "existing-exact.jpg"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO memorykeeper_file_states (
                    file_id,
                    user_capture_datetime,
                    user_capture_precision,
                    effective_capture_datetime,
                    date_basis
                ) VALUES (
                    :file_id,
                    :captured_at,
                    'DATE',
                    :captured_at,
                    'USER'
                )
                """
            ),
            {"file_id": common_file_id, "captured_at": captured_at},
        )

    run_with_engine_patch(migration_engine_factory, lambda: run_upgrade("head"))

    with postgresql_engine.connect() as connection:
        preserved = connection.execute(
            text(
                """
                SELECT effective_capture_datetime,
                       effective_capture_date,
                       effective_capture_year,
                       effective_capture_year_v2,
                       effective_capture_precision,
                       date_basis,
                       source_capture_year
                FROM memorykeeper_file_states
                WHERE file_id = :file_id
                """
            ),
            {"file_id": common_file_id},
        ).one()
        assert preserved == (
            captured_at,
            captured_at.date(),
            2022,
            2022,
            "DATE",
            "USER",
            None,
        )
        connection.rollback()
