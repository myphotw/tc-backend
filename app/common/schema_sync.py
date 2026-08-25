"""DB 스키마 동기화 (create_all + 누락 컬럼 ALTER)."""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateColumn

logger = logging.getLogger(__name__)

SERVICE_LINK_BACKFILL_MARKER = "common_file_services_legacy_backfill_v1"


def initialize_database(bind: Engine | None = None) -> list[str]:
    """
    테이블 생성 후 모델에만 있는 누락 컬럼/인덱스를 추가한다.

    Alembic 없이 create_all만 사용하면 기존 테이블에 새 컬럼이 추가되지 않는다.
    기존 데이터는 유지한 채 ALTER TABLE ADD COLUMN만 수행한다.

    Returns:
        list[str]: 적용된 변경 설명 목록
    """
    from app.common.database import Base, engine as default_engine

    engine = bind or default_engine
    before_create = inspect(engine)
    common_files_existed = before_create.has_table("common_files")
    service_links_existed = before_create.has_table("common_file_services")
    Base.metadata.create_all(bind=engine)
    changes = sync_missing_columns(engine)
    changes.extend(sync_missing_indexes(engine))
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


def sync_missing_columns(engine: Engine) -> list[str]:
    """모델 컬럼 중 DB에 없는 것을 ADD COLUMN 한다."""
    from app.common.database import Base

    inspector = inspect(engine)
    changes: list[str] = []

    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name, schema=table.schema):
                continue

            existing_columns = {
                column["name"]
                for column in inspector.get_columns(table.name, schema=table.schema)
            }

            for column in table.columns:
                if column.name in existing_columns:
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

    return changes


def sync_missing_indexes(engine: Engine) -> list[str]:
    """모델 인덱스 중 DB에 없는 것을 생성한다."""
    from app.common.database import Base

    inspector = inspect(engine)
    changes: list[str] = []

    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
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

    return changes


def verify_model_columns(engine: Engine, table_name: str) -> dict[str, object]:
    """
    특정 테이블의 모델 컬럼과 DB 컬럼을 비교한다.

    Returns:
        dict: model_columns / db_columns / missing_in_db / extra_in_db
    """
    from app.common.database import Base

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
