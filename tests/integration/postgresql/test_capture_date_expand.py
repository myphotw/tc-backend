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

EXPAND_REVISION = "20260901_0002"


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
        "effective_capture_datetime": DateTime,
        "effective_capture_date": Date,
        "effective_capture_year": Integer,
        "date_basis": String,
    }.items():
        column = state_columns[name]
        assert column["nullable"] is True
        assert isinstance(column["type"], column_type)
        if column_type is String:
            assert getattr(column["type"], "length", None) == 16

    assert state_columns["user_capture_datetime"]["type"].timezone is False
    assert state_columns["effective_capture_datetime"]["type"].timezone is False
    for name in ("effective_capture_date", "effective_capture_year"):
        computed = state_columns[name].get("computed")
        assert computed is not None
        assert computed.get("persisted") is True
        assert "effective_capture_datetime" in computed.get("sqltext", "")

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
                    effective_capture_datetime
                ) VALUES (:file_id, :captured_at)
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
                SELECT effective_capture_date, effective_capture_year
                FROM memorykeeper_file_states
                WHERE file_id = :file_id
                """
            ),
            {"file_id": common_file_id},
        ).one()
        assert derived == (date(2024, 2, 29), 2024)
        revision = connection.execute(
            text("SELECT version_num FROM public.alembic_version")
        ).scalar_one()
        assert revision == EXPAND_REVISION
        connection.rollback()

    with Session(postgresql_engine) as session:
        state = session.get(MemoryKeeperFileState, common_file_id)
        assert state is not None
        assert state.effective_capture_datetime == datetime(2024, 2, 29, 23, 45, 12)
        assert state.effective_capture_date == date(2024, 2, 29)
        assert state.effective_capture_year == 2024

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
        "effective_capture_datetime",
        "effective_capture_date",
        "effective_capture_year",
        "date_basis",
    }.isdisjoint(after_state)


def test_capture_date_model_ownership_is_verifiable() -> None:
    checks = verify_ownership_boundary()
    assert any(check.startswith("migration_scoped_tables=") for check in checks)
