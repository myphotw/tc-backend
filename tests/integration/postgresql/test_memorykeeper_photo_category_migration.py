from __future__ import annotations

from alembic import command
import pytest
from sqlalchemy import CheckConstraint, inspect, text
from sqlalchemy.engine import Engine

from app.common.model_registry import Base
from app.common.schema_sync import is_migration_managed
from scripts.db_migrate import build_alembic_config, run_stamp_baseline, run_upgrade
from tests.integration.postgresql.support import (
    create_legacy_schema,
    run_with_engine_patch,
)


pytestmark = pytest.mark.postgresql_integration


PREVIOUS_HEAD = "20260910_0006"
PHOTO_CATEGORY_REVISION = "20260914_0007"
TABLE_NAME = "memorykeeper_file_states"


def test_photo_category_upgrade_defaults_constraint_ownership_and_downgrade(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    create_legacy_schema(postgresql_engine)
    with postgresql_engine.begin() as connection:
        common_file_id = connection.execute(
            text(
                """
                INSERT INTO common_files (file_id, original_name, deleted)
                VALUES (:file_id, :original_name, false)
                RETURNING id
                """
            ),
            {"file_id": "7" * 64, "original_name": "existing.jpg"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO memorykeeper_file_states (file_id)
                VALUES (:file_id)
                """
            ),
            {"file_id": common_file_id},
        )

    run_with_engine_patch(migration_engine_factory, run_stamp_baseline)
    run_with_engine_patch(
        migration_engine_factory,
        lambda: run_upgrade(PHOTO_CATEGORY_REVISION),
    )

    inspector = inspect(postgresql_engine)
    columns = {
        column["name"]: column
        for column in inspector.get_columns(TABLE_NAME, schema="public")
    }
    assert columns["photo_category"]["nullable"] is False
    assert "NORMAL" in str(columns["photo_category"]["default"])
    assert columns["photo_category_revision"]["nullable"] is False
    assert str(columns["photo_category_revision"]["default"]).strip("()") == "0"
    constraints = {
        item["name"]: item
        for item in inspector.get_check_constraints(TABLE_NAME, schema="public")
    }
    sqltext = constraints[
        "ck_memorykeeper_file_states_photo_category"
    ]["sqltext"]
    assert "NORMAL" in sqltext
    assert "DAILY" in sqltext

    source = Base.metadata.tables[TABLE_NAME]
    assert is_migration_managed(source.c.photo_category)
    assert is_migration_managed(source.c.photo_category_revision)
    assert not any(
        isinstance(item, CheckConstraint)
        and item.name == "ck_memorykeeper_file_states_photo_category"
        for item in source.constraints
    )

    with postgresql_engine.connect() as connection:
        stored = connection.execute(
            text(
                """
                SELECT photo_category, photo_category_revision
                FROM memorykeeper_file_states
                WHERE file_id = :file_id
                """
            ),
            {"file_id": common_file_id},
        ).one()
        assert stored == ("NORMAL", 0)
        revision = connection.execute(
            text("SELECT version_num FROM public.alembic_version")
        ).scalar_one()
        assert revision == PHOTO_CATEGORY_REVISION
        connection.rollback()

        config = build_alembic_config()
        config.attributes["connection"] = connection
        command.downgrade(config, PREVIOUS_HEAD)
        connection.commit()

    downgraded_columns = {
        column["name"]
        for column in inspect(postgresql_engine).get_columns(
            TABLE_NAME,
            schema="public",
        )
    }
    assert "photo_category" not in downgraded_columns
    assert "photo_category_revision" not in downgraded_columns
