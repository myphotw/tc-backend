from __future__ import annotations

from alembic import command
import pytest
from sqlalchemy import Numeric, inspect
from sqlalchemy.engine import Engine

from app.common.model_registry import Base
from app.common.schema_sync import is_migration_managed
from scripts.db_migrate import build_alembic_config, run_stamp_baseline, run_upgrade
from tests.integration.postgresql.support import (
    create_legacy_schema,
    run_with_engine_patch,
)


pytestmark = pytest.mark.postgresql_integration


PREVIOUS_HEAD = "20260906_0004"
MASTER_DATA_REVISION = "20260909_0005"
MASTER_TABLES = {
    "astro_equipment",
    "astro_equipment_eyepieces",
    "astro_equipment_exposure_capabilities",
    "astro_equipment_exposure_values",
    "astro_observation_sites",
    "astro_observation_site_horizon_points",
    "astro_observation_site_blocked_azimuth_ranges",
}


def test_astro_master_data_upgrade_schema_ownership_and_downgrade(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    create_legacy_schema(postgresql_engine)
    run_with_engine_patch(migration_engine_factory, run_stamp_baseline)
    run_with_engine_patch(
        migration_engine_factory,
        lambda: run_upgrade(MASTER_DATA_REVISION),
    )

    inspector = inspect(postgresql_engine)
    for table_name in MASTER_TABLES:
        assert inspector.has_table(table_name, schema="public")
        assert is_migration_managed(Base.metadata.tables[table_name])

    value_columns = {
        column["name"]: column
        for column in inspector.get_columns(
            "astro_equipment_exposure_values",
            schema="public",
        )
    }
    value_type = value_columns["value_seconds"]["type"]
    assert isinstance(value_type, Numeric)
    assert value_type.precision == 12
    assert value_type.scale == 6

    site_foreign_keys = inspector.get_foreign_keys(
        "astro_observation_sites",
        schema="public",
    )
    default_equipment_fk = next(
        item
        for item in site_foreign_keys
        if item["constrained_columns"] == ["default_equipment_id"]
    )
    assert default_equipment_fk["referred_table"] == "astro_equipment"
    assert default_equipment_fk["options"]["ondelete"] == "SET NULL"

    with postgresql_engine.connect() as connection:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM public.alembic_version"
        ).scalar_one()
        assert revision == MASTER_DATA_REVISION
        connection.rollback()

        config = build_alembic_config()
        config.attributes["connection"] = connection
        command.downgrade(config, PREVIOUS_HEAD)
        connection.commit()

    downgraded = inspect(postgresql_engine)
    for table_name in MASTER_TABLES:
        assert not downgraded.has_table(table_name, schema="public")
