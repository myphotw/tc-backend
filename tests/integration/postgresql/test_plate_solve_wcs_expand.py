from __future__ import annotations

from alembic import command
import pytest
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

from app.astrojournal.models.plate_solve_job import AstroPlateSolveJob
from app.common.schema_sync import is_migration_managed
from scripts.db_migrate import build_alembic_config, run_stamp_baseline, run_upgrade
from tests.integration.postgresql.support import (
    create_legacy_schema,
    run_with_engine_patch,
)


pytestmark = pytest.mark.postgresql_integration

PREVIOUS_HEAD = "20260901_0003"
WCS_REVISION = "20260906_0004"


def test_plate_solve_wcs_upgrade_reflection_ownership_and_downgrade(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    create_legacy_schema(postgresql_engine)
    run_with_engine_patch(migration_engine_factory, run_stamp_baseline)
    run_with_engine_patch(
        migration_engine_factory,
        lambda: run_upgrade(WCS_REVISION),
    )

    columns = {
        column["name"]: column
        for column in inspect(postgresql_engine).get_columns(
            "astro_plate_solve_jobs",
            schema="public",
        )
    }
    wcs = columns["wcs"]
    assert isinstance(wcs["type"], JSONB)
    assert wcs["nullable"] is True
    assert is_migration_managed(AstroPlateSolveJob.__table__.c.wcs)

    with postgresql_engine.connect() as connection:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM public.alembic_version"
        ).scalar_one()
        assert revision == WCS_REVISION
        connection.rollback()

        config = build_alembic_config()
        config.attributes["connection"] = connection
        command.downgrade(config, PREVIOUS_HEAD)
        connection.commit()

    downgraded = {
        column["name"]
        for column in inspect(postgresql_engine).get_columns(
            "astro_plate_solve_jobs",
            schema="public",
        )
    }
    assert "wcs" not in downgraded

