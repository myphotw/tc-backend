"""DB 스키마 동기화 (create_all + 누락 컬럼 ALTER)."""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateColumn

logger = logging.getLogger(__name__)


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
    Base.metadata.create_all(bind=engine)
    changes = sync_missing_columns(engine)
    changes.extend(sync_missing_indexes(engine))
    if changes:
        logger.info("Database schema synced: %s", changes)
    else:
        logger.info("Database schema already up to date")
    return changes


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
