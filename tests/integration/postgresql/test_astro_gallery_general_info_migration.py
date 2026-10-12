from __future__ import annotations

from alembic import command
import pytest
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from scripts.db_migrate import build_alembic_config, run_stamp_baseline, run_upgrade
from tests.integration.postgresql.support import (
    create_legacy_schema,
    run_with_engine_patch,
)


pytestmark = pytest.mark.postgresql_integration


PREVIOUS_HEAD = "20260925_0008"
GENERAL_INFO_REVISION = "20261012_0009"
TABLE_NAME = "astro_observation_records"


def test_general_info_upgrade_constraints_and_downgrade(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    create_legacy_schema(postgresql_engine)
    run_with_engine_patch(migration_engine_factory, run_stamp_baseline)
    run_with_engine_patch(
        migration_engine_factory,
        lambda: run_upgrade(GENERAL_INFO_REVISION),
    )

    inspector = inspect(postgresql_engine)
    columns = {
        item["name"]: item
        for item in inspector.get_columns(TABLE_NAME, schema="public")
    }
    for column_name in (
        "equipment_ref_id",
        "equipment_name_snapshot",
        "integration_seconds",
        "address",
    ):
        assert columns[column_name]["nullable"]

    foreign_keys = inspector.get_foreign_keys(TABLE_NAME, schema="public")
    equipment_fk = next(
        item
        for item in foreign_keys
        if item["name"] == "fk_astro_observation_records_equipment_ref_id"
    )
    assert equipment_fk["constrained_columns"] == ["equipment_ref_id"]
    assert equipment_fk["referred_table"] == "astro_equipment"
    assert equipment_fk["options"]["ondelete"] == "SET NULL"

    checks = {
        item["name"]: item
        for item in inspector.get_check_constraints(TABLE_NAME, schema="public")
    }
    assert "ck_astro_observation_records_integration_seconds" in checks
    indexes = {
        item["name"]: item
        for item in inspector.get_indexes(TABLE_NAME, schema="public")
    }
    assert indexes["ix_astro_observation_records_equipment_ref_id"][
        "column_names"
    ] == ["equipment_ref_id"]

    with postgresql_engine.connect() as connection:
        config = build_alembic_config()
        config.attributes["connection"] = connection
        command.downgrade(config, PREVIOUS_HEAD)
        connection.commit()

    downgraded_columns = {
        item["name"]
        for item in inspect(postgresql_engine).get_columns(
            TABLE_NAME,
            schema="public",
        )
    }
    assert "equipment_ref_id" not in downgraded_columns
    assert "equipment_name_snapshot" not in downgraded_columns
    assert "integration_seconds" not in downgraded_columns
    assert "address" not in downgraded_columns
