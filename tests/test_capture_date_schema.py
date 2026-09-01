from sqlalchemy import Computed, Date, DateTime, FetchedValue, Integer, String
from sqlalchemy import create_engine, inspect

from app.common.model_registry import Base
from app.common.models.file_metadata import CommonFileMetadata
from app.common.schema_sync import (
    bootstrap_managed_tables,
    initialize_database,
    is_migration_managed,
)
from app.memorykeeper.models.file_state import MemoryKeeperFileState


def test_capture_date_columns_are_nullable_migration_owned_model_schema() -> None:
    original = CommonFileMetadata.__table__.c.original_capture_datetime
    assert isinstance(original.type, DateTime)
    assert original.type.timezone is False
    assert original.nullable is True
    assert is_migration_managed(original)

    expected = {
        "user_capture_datetime": DateTime,
        "user_capture_precision": String,
        "effective_capture_datetime": DateTime,
        "effective_capture_date": Date,
        "effective_capture_year": Integer,
        "date_basis": String,
    }
    for name, column_type in expected.items():
        column = MemoryKeeperFileState.__table__.c[name]
        assert isinstance(column.type, column_type)
        assert column.nullable is True
        assert is_migration_managed(column)

    assert MemoryKeeperFileState.__table__.c.user_capture_datetime.type.timezone is False
    assert MemoryKeeperFileState.__table__.c.effective_capture_datetime.type.timezone is False

    for name in ("effective_capture_date", "effective_capture_year"):
        column = MemoryKeeperFileState.__table__.c[name]
        assert isinstance(column.server_default, FetchedValue)
        assert isinstance(column.server_onupdate, FetchedValue)

    assert not any(
        isinstance(column.computed, Computed)
        for table in Base.metadata.tables.values()
        for column in table.columns
    )


def test_capture_date_columns_are_excluded_from_startup_ddl_not_base_tables() -> None:
    metadata = CommonFileMetadata.metadata
    bootstrap_tables = set(bootstrap_managed_tables(metadata))

    assert CommonFileMetadata.__table__ in bootstrap_tables
    assert MemoryKeeperFileState.__table__ in bootstrap_tables

    engine = create_engine("sqlite:///:memory:")
    try:
        initialize_database(engine)
        inspector = inspect(engine)
        assert inspector.has_table("common_file_metadata")
        assert inspector.has_table("memorykeeper_file_states")

        metadata_columns = {
            column["name"]
            for column in inspector.get_columns("common_file_metadata")
        }
        assert {"id", "file_id", "datetime_original"} <= metadata_columns
        assert "original_capture_datetime" not in metadata_columns

        state_columns = {
            column["name"]
            for column in inspector.get_columns("memorykeeper_file_states")
        }
        assert {"file_id", "favorite", "memo", "revision"} <= state_columns
        assert {
            "user_capture_datetime",
            "user_capture_precision",
            "effective_capture_datetime",
            "effective_capture_date",
            "effective_capture_year",
            "date_basis",
        }.isdisjoint(state_columns)
        index_names = {
            index["name"]
            for index in inspector.get_indexes("memorykeeper_file_states")
        }
        assert "ix_memorykeeper_file_states_effective_capture_desc" not in index_names
    finally:
        engine.dispose()


def test_shared_metadata_creates_on_sqlite_without_postgresql_computed_sql() -> None:
    engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        columns = {
            column["name"]
            for column in inspect(engine).get_columns("memorykeeper_file_states")
        }
        assert {"effective_capture_date", "effective_capture_year"}.issubset(columns)
    finally:
        engine.dispose()


def test_fast_gallery_keyset_index_is_migration_owned() -> None:
    index = next(
        index
        for index in MemoryKeeperFileState.__table__.indexes
        if index.name == "ix_memorykeeper_file_states_effective_capture_desc"
    )
    assert is_migration_managed(index)
