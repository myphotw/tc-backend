from __future__ import annotations

from alembic import command
import pytest
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

from scripts.db_migrate import build_alembic_config, run_stamp_baseline, run_upgrade
from tests.integration.postgresql.support import (
    create_legacy_schema,
    run_with_engine_patch,
)


pytestmark = pytest.mark.postgresql_integration


PREVIOUS_HEAD = "20261021_0010"
CAPTURE_CAPABILITY_REVISION = "20261028_0011"
TABLE_NAME = "astro_equipment"
COLUMN_NAME = "capture_capability"


def test_capture_capability_jsonb_upgrade_and_downgrade(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    create_legacy_schema(postgresql_engine)
    run_with_engine_patch(migration_engine_factory, run_stamp_baseline)
    run_with_engine_patch(
        migration_engine_factory,
        lambda: run_upgrade(CAPTURE_CAPABILITY_REVISION),
    )

    columns = {
        item["name"]: item
        for item in inspect(postgresql_engine).get_columns(TABLE_NAME, schema="public")
    }
    column = columns[COLUMN_NAME]
    assert isinstance(column["type"], JSONB)
    assert column["nullable"]
    assert column["default"] is None

    with postgresql_engine.connect() as connection:
        config = build_alembic_config()
        config.attributes["connection"] = connection
        command.downgrade(config, PREVIOUS_HEAD)
        connection.commit()

    downgraded_columns = {
        item["name"]
        for item in inspect(postgresql_engine).get_columns(TABLE_NAME, schema="public")
    }
    assert COLUMN_NAME not in downgraded_columns
