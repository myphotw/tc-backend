from __future__ import annotations

from alembic import command
import pytest
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from app.common.model_registry import Base
from app.common.schema_sync import is_migration_managed
from scripts.db_migrate import build_alembic_config, run_stamp_baseline, run_upgrade
from tests.integration.postgresql.support import (
    create_legacy_schema,
    run_with_engine_patch,
)


pytestmark = pytest.mark.postgresql_integration


PREVIOUS_HEAD = "20260909_0005"
MULTI_NIGHT_REVISION = "20260910_0006"
TABLE_NAME = "astro_multi_night_framing_references"


def test_multi_night_reference_upgrade_constraints_ownership_and_downgrade(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    create_legacy_schema(postgresql_engine)
    run_with_engine_patch(migration_engine_factory, run_stamp_baseline)
    run_with_engine_patch(
        migration_engine_factory,
        lambda: run_upgrade(MULTI_NIGHT_REVISION),
    )

    inspector = inspect(postgresql_engine)
    assert inspector.has_table(TABLE_NAME, schema="public")
    assert is_migration_managed(Base.metadata.tables[TABLE_NAME])

    foreign_keys = inspector.get_foreign_keys(TABLE_NAME, schema="public")
    by_column = {
        tuple(item["constrained_columns"]): item
        for item in foreign_keys
    }
    assert by_column[("site_id",)]["referred_table"] == "astro_observation_sites"
    assert by_column[("site_id",)]["options"]["ondelete"] == "RESTRICT"
    assert by_column[("equipment_id",)]["referred_table"] == "astro_equipment"
    assert by_column[("equipment_id",)]["options"]["ondelete"] == "RESTRICT"

    indexes = {
        item["name"]: item
        for item in inspector.get_indexes(TABLE_NAME, schema="public")
    }
    unique_index = indexes["uq_astro_multi_night_active_target_equipment"]
    assert unique_index["unique"]
    assert unique_index["column_names"] == ["catalog_object_id", "equipment_id"]
    assert "deleted_at IS NULL" in str(
        unique_index["dialect_options"]["postgresql_where"]
    )

    with postgresql_engine.connect() as connection:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM public.alembic_version"
        ).scalar_one()
        assert revision == MULTI_NIGHT_REVISION
        connection.rollback()

        config = build_alembic_config()
        config.attributes["connection"] = connection
        command.downgrade(config, PREVIOUS_HEAD)
        connection.commit()

    assert not inspect(postgresql_engine).has_table(TABLE_NAME, schema="public")
