"""DB 스키마 동기화와 startup auto-DDL ownership 경계."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy import MetaData, Table, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateColumn

logger = logging.getLogger(__name__)

SERVICE_LINK_BACKFILL_MARKER = "common_file_services_legacy_backfill_v1"
SCHEMA_OWNER_INFO_KEY = "tc_schema_owner"
BOOTSTRAP_SCHEMA_OWNER = "bootstrap"
MIGRATION_SCHEMA_OWNER = "migration"
_VALID_SCHEMA_OWNERS = {
    BOOTSTRAP_SCHEMA_OWNER,
    MIGRATION_SCHEMA_OWNER,
}


def migration_managed_schema_info() -> dict[str, str]:
    """Return SQLAlchemy ``info`` metadata for migration-owned schema items.

    Apply this to a ``Column``, ``Index``, or ``Table``. Items without an
    explicit owner remain bootstrap-managed for backward compatibility.
    """
    return {SCHEMA_OWNER_INFO_KEY: MIGRATION_SCHEMA_OWNER}


def is_migration_managed(schema_item: object) -> bool:
    """Return whether a SQLAlchemy schema item is owned by migrations.

    Table ownership is inherited by its columns and indexes. A misspelled
    explicit owner fails closed instead of silently enabling startup DDL.
    """
    if _declared_schema_owner(schema_item) == MIGRATION_SCHEMA_OWNER:
        return True

    parent_table = getattr(schema_item, "table", None)
    return (
        parent_table is not None
        and parent_table is not schema_item
        and _declared_schema_owner(parent_table) == MIGRATION_SCHEMA_OWNER
    )


def bootstrap_managed_tables(metadata: MetaData) -> tuple[Table, ...]:
    """Return tables that ``create_all`` may create at application startup.

    Table ownership is independent from child Column and Index ownership.
    A bootstrap-owned table remains creatable even when it contains
    migration-owned children; ``bootstrap_metadata_projection`` omits those
    children from its startup DDL.
    """
    return tuple(
        table
        for table in metadata.sorted_tables
        if not is_migration_managed(table)
    )


def table_has_migration_managed_schema(table: Table) -> bool:
    """Return whether a table or one of its direct schema children is managed.

    Alembic uses this to traverse mixed-ownership tables so it can consider
    their migration-owned columns and indexes. Startup bootstrap instead uses
    table ownership plus a projection that removes those child DDL elements.
    """
    return is_migration_managed(table) or any(
        is_migration_managed(item)
        for item in (*table.columns, *table.indexes)
    )


def bootstrap_metadata_projection(metadata: MetaData) -> MetaData:
    """Build independent startup DDL metadata without migration-owned children.

    The source ``Base.metadata`` remains untouched for ORM mapping and Alembic.
    ``Table.to_metadata`` preserves the bootstrap table's public schema
    definition; only copied migration-owned columns are marked as SQLAlchemy
    system columns, which excludes them from ``CREATE TABLE`` rendering.
    """
    projection = MetaData(
        schema=metadata.schema,
        naming_convention=metadata.naming_convention,
        info=dict(metadata.info),
    )
    copied_tables = [
        (table, table.to_metadata(projection))
        for table in bootstrap_managed_tables(metadata)
    ]

    for source_table, projected_table in copied_tables:
        migration_column_names = {
            column.name
            for column in source_table.columns
            if is_migration_managed(column)
        }
        _validate_bootstrap_projection_constraints(
            source_table,
            migration_column_names,
        )
        for column_name in migration_column_names:
            # ``system`` is set only on this independent DDL projection. It
            # causes SQLAlchemy to omit the copied column from CREATE TABLE
            # without mutating the ORM/Alembic source metadata.
            projected_table.c[column_name].system = True

        migration_index_names = {
            index.name
            for index in source_table.indexes
            if is_migration_managed(index)
        }
        if None in migration_index_names:
            raise ValueError(
                "Migration-managed indexes require explicit names for "
                "bootstrap projection"
            )
        _validate_bootstrap_projection_indexes(
            source_table,
            migration_column_names,
        )
        for index in tuple(projected_table.indexes):
            if index.name in migration_index_names:
                projected_table.indexes.remove(index)

    return projection


def initialize_database(bind: Engine | None = None) -> list[str]:
    """
    테이블 생성 후 모델에만 있는 누락 컬럼/인덱스를 추가한다.

    Alembic 없이 create_all만 사용하면 기존 테이블에 새 컬럼이 추가되지 않는다.
    기존 데이터는 유지한 채 ALTER TABLE ADD COLUMN만 수행한다.

    Returns:
        list[str]: 적용된 변경 설명 목록
    """
    from app.common.model_registry import Base

    if bind is None:
        from app.common.database import engine as default_engine

        engine = default_engine
    else:
        engine = bind
    metadata = Base.metadata
    before_create = inspect(engine)
    common_files_existed = before_create.has_table("common_files")
    service_links_existed = before_create.has_table("common_file_services")
    auto_create_tables = bootstrap_managed_tables(metadata)
    _log_ownership_summary(metadata, auto_create_tables)
    bootstrap_metadata_projection(metadata).create_all(bind=engine)
    changes = sync_missing_columns(engine, metadata=metadata)
    changes.extend(sync_missing_indexes(engine, metadata=metadata))
    changes.extend(
        sync_file_service_links(
            engine,
            allow_legacy_backfill=(
                common_files_existed and not service_links_existed
            ),
        )
    )
    if changes:
        logger.info("Database schema synced: %s", changes)
    else:
        logger.info("Database schema already up to date")
    return changes


def sync_file_service_links(
    engine: Engine,
    *,
    allow_legacy_backfill: bool = False,
) -> list[str]:
    """Run the pre-B2 service-link migration at most once.

    ``common_files.service_name`` is legacy compatibility data, not current
    ownership. It may be copied only when startup observed a pre-B2 database:
    ``common_files`` existed before ``create_all`` while
    ``common_file_services`` did not. If the link table already existed, an
    absent link can be an intentional Reset/delete and is never reconstructed.
    """
    inspector = inspect(engine)
    if not (
        inspector.has_table("common_files")
        and inspector.has_table("common_file_services")
        and inspector.has_table("common_settings")
    ):
        return []

    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:migration_key))"),
                {"migration_key": SERVICE_LINK_BACKFILL_MARKER},
            )
        completed = connection.execute(
            text(
                """
                SELECT 1
                FROM common_settings
                WHERE setting_key = :migration_key
                """
            ),
            {"migration_key": SERVICE_LINK_BACKFILL_MARKER},
        ).first()
        if completed is not None:
            logger.debug("Legacy service-link migration already completed")
            return []

        inserted = 0
        if allow_legacy_backfill:
            result = connection.execute(
                text(
                    """
                    INSERT INTO common_file_services (file_id, service_name)
                    SELECT files.id, files.service_name
                    FROM common_files AS files
                    WHERE files.service_name IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1
                        FROM common_file_services AS links
                        WHERE links.file_id = files.id
                          AND links.service_name = files.service_name
                    )
                    """
                )
            )
            inserted = result.rowcount or 0

        connection.execute(
            text(
                """
                INSERT INTO common_settings (
                    category,
                    setting_key,
                    setting_value,
                    description
                ) VALUES (
                    'MIGRATION',
                    :migration_key,
                    'COMPLETED',
                    :description
                )
                """
            ),
            {
                "migration_key": SERVICE_LINK_BACKFILL_MARKER,
                "description": (
                    "One-time pre-B2 common_file_services legacy backfill"
                ),
            },
        )

    if not allow_legacy_backfill:
        logger.info(
            "Marked legacy service-link migration complete without backfill; "
            "common_file_services already existed"
        )
    return [f"backfill:common_file_services={inserted}"] if inserted else []


def sync_missing_columns(
    engine: Engine,
    *,
    metadata: MetaData | None = None,
) -> list[str]:
    """모델 컬럼 중 DB에 없는 것을 ADD COLUMN 한다."""
    metadata = _resolve_metadata(metadata)

    inspector = inspect(engine)
    changes: list[str] = []
    migration_skips: list[str] = []

    with engine.begin() as connection:
        for table in metadata.sorted_tables:
            if not inspector.has_table(table.name, schema=table.schema):
                continue

            existing_columns = {
                column["name"]
                for column in inspector.get_columns(table.name, schema=table.schema)
            }

            for column in table.columns:
                if column.name in existing_columns:
                    continue
                if is_migration_managed(column):
                    migration_skips.append(f"{table.name}.{column.name}")
                    continue

                column_ddl = str(
                    CreateColumn(column).compile(dialect=engine.dialect)
                ).strip()
                table_name = _quote_ident(table.name, engine)
                ddl = f"ALTER TABLE {table_name} ADD COLUMN {column_ddl}"
                connection.execute(text(ddl))
                existing_columns.add(column.name)
                changes.append(f"{table.name}.{column.name}")
                logger.warning("Added missing column: %s.%s", table.name, column.name)

    _log_migration_skips("columns", migration_skips)
    return changes


def sync_missing_indexes(
    engine: Engine,
    *,
    metadata: MetaData | None = None,
) -> list[str]:
    """모델 인덱스 중 DB에 없는 것을 생성한다."""
    metadata = _resolve_metadata(metadata)

    inspector = inspect(engine)
    changes: list[str] = []
    migration_skips: list[str] = []

    with engine.begin() as connection:
        for table in metadata.sorted_tables:
            if not inspector.has_table(table.name, schema=table.schema):
                continue

            existing_index_names = {
                index["name"]
                for index in inspector.get_indexes(table.name, schema=table.schema)
                if index.get("name")
            }
            existing_index_names.update(
                {
                    constraint["name"]
                    for constraint in inspector.get_unique_constraints(
                        table.name,
                        schema=table.schema,
                    )
                    if constraint.get("name")
                }
            )

            for index in table.indexes:
                if not index.name or index.name in existing_index_names:
                    continue
                if is_migration_managed(index):
                    migration_skips.append(index.name)
                    continue
                try:
                    index.create(bind=connection)
                    changes.append(f"index:{index.name}")
                    logger.warning("Created missing index: %s", index.name)
                except Exception as exc:
                    # 동일 컬럼 인덱스가 다른 이름으로 이미 있을 수 있다.
                    logger.warning(
                        "Skip creating index %s: %s",
                        index.name,
                        exc,
                    )

    _log_migration_skips("indexes", migration_skips)
    return changes


def verify_model_columns(engine: Engine, table_name: str) -> dict[str, object]:
    """
    특정 테이블의 모델 컬럼과 DB 컬럼을 비교한다.

    Returns:
        dict: model_columns / db_columns / missing_in_db / extra_in_db
    """
    from app.common.model_registry import Base

    table = Base.metadata.tables[table_name]
    inspector = inspect(engine)
    model_columns = {column.name for column in table.columns}
    db_columns = {
        column["name"]
        for column in inspector.get_columns(table_name)
    }
    return {
        "table": table_name,
        "model_columns": sorted(model_columns),
        "db_columns": sorted(db_columns),
        "missing_in_db": sorted(model_columns - db_columns),
        "extra_in_db": sorted(db_columns - model_columns),
    }


def _quote_ident(name: str, engine: Engine) -> str:
    """엔진 dialect에 맞게 identifier를 quote한다."""
    return engine.dialect.identifier_preparer.quote(name)


def _resolve_metadata(metadata: MetaData | None) -> MetaData:
    if metadata is not None:
        return metadata

    from app.common.model_registry import Base

    return Base.metadata


def _declared_schema_owner(schema_item: object) -> str:
    info = getattr(schema_item, "info", {}) or {}
    owner = info.get(SCHEMA_OWNER_INFO_KEY, BOOTSTRAP_SCHEMA_OWNER)
    if owner not in _VALID_SCHEMA_OWNERS:
        item_name = getattr(schema_item, "name", repr(schema_item))
        raise ValueError(
            f"Unknown schema owner {owner!r} for {item_name!r}; "
            f"expected one of {sorted(_VALID_SCHEMA_OWNERS)}"
        )
    return owner


def _validate_bootstrap_projection_constraints(
    table: Table,
    migration_column_names: set[str],
) -> None:
    """Reject constraints that would reference omitted startup DDL columns."""
    if not migration_column_names:
        return
    for constraint in table.constraints:
        column_names = {
            column.name
            for column in getattr(constraint, "columns", ())
            if column.name is not None
        }
        overlap = column_names & migration_column_names
        if overlap:
            raise ValueError(
                "Bootstrap constraint references migration-managed column(s): "
                f"{table.fullname}.{constraint.name or type(constraint).__name__} "
                f"-> {sorted(overlap)}"
            )


def _validate_bootstrap_projection_indexes(
    table: Table,
    migration_column_names: set[str],
) -> None:
    """Reject unmarked indexes that would target omitted startup columns."""
    if not migration_column_names:
        return
    for index in table.indexes:
        if is_migration_managed(index):
            continue
        column_names = {column.name for column in index.columns}
        overlap = column_names & migration_column_names
        if overlap:
            raise ValueError(
                "Bootstrap index references migration-managed column(s): "
                f"{table.fullname}.{index.name or '<unnamed>'} -> {sorted(overlap)}"
            )


def _log_ownership_summary(
    metadata: MetaData,
    auto_create_tables: Iterable[Table],
) -> None:
    auto_table_names = {table.fullname for table in auto_create_tables}
    migration_tables = [
        table.fullname
        for table in metadata.sorted_tables
        if is_migration_managed(table)
    ]
    mixed_tables = [
        table.fullname
        for table in metadata.sorted_tables
        if not is_migration_managed(table)
        and table_has_migration_managed_schema(table)
    ]
    migration_columns = [
        f"{table.fullname}.{column.name}"
        for table in metadata.sorted_tables
        for column in table.columns
        if is_migration_managed(column)
    ]
    migration_indexes = [
        index.name or f"{table.fullname}:unnamed"
        for table in metadata.sorted_tables
        for index in table.indexes
        if is_migration_managed(index)
    ]
    total_columns = sum(len(table.columns) for table in metadata.sorted_tables)
    total_indexes = sum(len(table.indexes) for table in metadata.sorted_tables)

    logger.info(
        "Schema sync ownership: auto-managed tables=%d columns=%d indexes=%d; "
        "migration-managed tables=%d mixed tables=%d columns=%d indexes=%d",
        len(auto_table_names),
        total_columns - len(migration_columns),
        total_indexes - len(migration_indexes),
        len(migration_tables),
        len(mixed_tables),
        len(migration_columns),
        len(migration_indexes),
    )
    if migration_tables or mixed_tables or migration_columns or migration_indexes:
        logger.debug(
            "Migration-managed schema excluded from startup auto-DDL: "
            "tables=%s mixed_tables=%s columns=%s indexes=%s",
            migration_tables,
            mixed_tables,
            migration_columns,
            migration_indexes,
        )


def _log_migration_skips(kind: str, names: list[str]) -> None:
    if not names:
        return
    logger.info(
        "Schema sync skipped missing migration-managed %s: count=%d",
        kind,
        len(names),
    )
    logger.debug(
        "Missing migration-managed %s excluded from startup auto-DDL: %s",
        kind,
        names,
    )
