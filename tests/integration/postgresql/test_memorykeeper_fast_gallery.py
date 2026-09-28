from __future__ import annotations

from datetime import datetime
import json

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.repositories.fast_gallery_repository import FastGalleryFilters
from app.memorykeeper.repositories.fast_gallery_repository import (
    MemoryKeeperFastGalleryRepository,
)
from app.memorykeeper.services.fast_gallery_service import MemoryKeeperFastGalleryService
from scripts.db_migrate import run_stamp_baseline, run_upgrade
from tests.integration.postgresql.support import create_legacy_schema, run_with_engine_patch


pytestmark = pytest.mark.postgresql_integration

HEAD_REVISION = "20260925_0008"
FAST_GALLERY_INDEX = "ix_memorykeeper_file_states_effective_capture_desc"


def _upgrade_to_head(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    create_legacy_schema(postgresql_engine)
    run_with_engine_patch(migration_engine_factory, run_stamp_baseline)
    run_with_engine_patch(migration_engine_factory, lambda: run_upgrade("head"))


def _add_photo(
    session: Session,
    *,
    suffix: int,
    captured_at: datetime | None,
    capture_year: int | None = None,
) -> CommonFile:
    common_file = CommonFile(
        file_id=f"{suffix:064x}",
        original_name=f"{suffix}.jpg",
        deleted=False,
    )
    session.add(common_file)
    session.flush()
    session.add(CommonFileService(file_id=common_file.id, service_name="MemoryKeeper"))
    session.add(CommonFileMetadata(file_id=common_file.id))
    session.add(
        MemoryKeeperFileState(
            file_id=common_file.id,
            effective_capture_datetime=captured_at,
            effective_capture_year=(
                capture_year
                if capture_year is not None
                else (captured_at.year if captured_at is not None else None)
            ),
            effective_capture_precision=(
                "DATETIME" if captured_at is not None else "YEAR"
            ),
            date_basis="EXIF" if captured_at is not None else "SOURCE_YEAR",
        )
    )
    session.flush()
    return common_file


def test_fast_gallery_migration_creates_partial_keyset_index(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    _upgrade_to_head(postgresql_engine, migration_engine_factory)

    indexes = {
        index["name"]: index
        for index in inspect(postgresql_engine).get_indexes(
            "memorykeeper_file_states",
            schema="public",
        )
    }
    assert FAST_GALLERY_INDEX in indexes
    assert indexes[FAST_GALLERY_INDEX]["column_names"] == [
        "effective_capture_datetime",
        "file_id",
    ]
    with postgresql_engine.connect() as connection:
        predicate = connection.execute(
            text(
                "SELECT pg_get_expr(indpred, indrelid) "
                "FROM pg_index WHERE indexrelid = CAST(:index_name AS regclass)"
            ),
            {"index_name": FAST_GALLERY_INDEX},
        ).scalar_one()
        revision = connection.execute(
            text("SELECT version_num FROM public.alembic_version")
        ).scalar_one()
        connection.rollback()
    assert "effective_capture_datetime" in predicate
    assert revision == HEAD_REVISION


def test_fast_gallery_keyset_uses_generated_capture_projection(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    _upgrade_to_head(postgresql_engine, migration_engine_factory)
    with Session(postgresql_engine) as session:
        first = _add_photo(session, suffix=1, captured_at=datetime(2025, 1, 1, 12, 0))
        second = _add_photo(session, suffix=2, captured_at=datetime(2025, 1, 1, 12, 0))
        third = _add_photo(session, suffix=3, captured_at=datetime(2024, 1, 1, 12, 0))
        session.commit()

        service = MemoryKeeperFastGalleryService(session)
        page_one = service.photos(cursor=None, limit=2, filters=FastGalleryFilters())
        assert [item.common_file_id for item in page_one.items] == [second.id, first.id]
        assert [item.effective_capture_year for item in page_one.items] == [2025, 2025]
        assert page_one.next_cursor is not None

        page_two = service.photos(
            cursor=page_one.next_cursor,
            limit=2,
            filters=FastGalleryFilters(),
        )
        assert [item.common_file_id for item in page_two.items] == [third.id]

        compiled = str(
            session.query(MemoryKeeperFileState)
            .filter(MemoryKeeperFileState.effective_capture_datetime.isnot(None))
            .order_by(
                MemoryKeeperFileState.effective_capture_datetime.desc(),
                MemoryKeeperFileState.file_id.desc(),
            )
            .statement.compile(dialect=postgresql_engine.dialect)
        )
        assert "ORDER BY memorykeeper_file_states.effective_capture_datetime DESC" in compiled


def test_fast_gallery_mixed_precision_keyset_crosses_null_and_year_boundaries(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    _upgrade_to_head(postgresql_engine, migration_engine_factory)
    with Session(postgresql_engine) as session:
        exact_2025 = _add_photo(
            session,
            suffix=1,
            captured_at=datetime(2025, 6, 1, 12, 0),
        )
        year_only_2025 = _add_photo(
            session,
            suffix=2,
            captured_at=None,
            capture_year=2025,
        )
        exact_2024 = _add_photo(
            session,
            suffix=3,
            captured_at=datetime(2024, 12, 31, 23, 59),
        )
        year_only_2024 = _add_photo(
            session,
            suffix=4,
            captured_at=None,
            capture_year=2024,
        )
        session.commit()

        service = MemoryKeeperFastGalleryService(session)
        first = service.photos(cursor=None, limit=2, filters=FastGalleryFilters())
        second = service.photos(
            cursor=first.next_cursor,
            limit=2,
            filters=FastGalleryFilters(),
        )

        delivered = [
            item.common_file_id for item in [*first.items, *second.items]
        ]
        assert delivered == [
            exact_2025.id,
            year_only_2025.id,
            exact_2024.id,
            year_only_2024.id,
        ]
        assert len(delivered) == len(set(delivered))


def test_fast_gallery_mixed_precision_plan_keeps_candidate_limit(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    _upgrade_to_head(postgresql_engine, migration_engine_factory)
    # A set-based fixture approximates current production cardinality without
    # making test runtime depend on 10,000 ORM flushes.
    with postgresql_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO common_files (
                    file_id, original_name, service_name, deleted
                )
                SELECT
                    lpad(to_hex(value), 64, '0'),
                    value::text || '.jpg',
                    'MemoryKeeper',
                    false
                FROM generate_series(1, 10000) AS generated(value)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO common_file_services (file_id, service_name)
                SELECT id, 'MemoryKeeper' FROM common_files
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO common_file_metadata (file_id)
                SELECT id FROM common_files
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO memorykeeper_file_states (
                    file_id,
                    effective_capture_datetime,
                    effective_capture_year_v2,
                    effective_capture_precision,
                    date_basis
                )
                SELECT
                    id,
                    TIMESTAMP '2025-01-01 00:00:00'
                        + id * INTERVAL '1 second',
                    2025,
                    'DATETIME',
                    'EXIF'
                FROM common_files
                """
            )
        )
        connection.execute(text("ANALYZE common_files"))
        connection.execute(text("ANALYZE common_file_services"))
        connection.execute(text("ANALYZE common_file_metadata"))
        connection.execute(text("ANALYZE memorykeeper_file_states"))

    with Session(postgresql_engine) as session:
        repository = MemoryKeeperFastGalleryRepository(session)
        first_statement = repository.build_photos_statement(
            filters=FastGalleryFilters(),
            limit=50,
            cursor_datetime=None,
            cursor_file_id=None,
        )
        cursor_statement = repository.build_photos_statement(
            filters=FastGalleryFilters(),
            limit=50,
            cursor_datetime=datetime(2025, 1, 1, 2, 45),
            cursor_file_id=9900,
        )

    with postgresql_engine.connect() as connection:
        first_plan = _explain_json(
            connection,
            first_statement,
            postgresql_engine,
        )
        cursor_plan = _explain_json(
            connection,
            cursor_statement,
            postgresql_engine,
        )
        connection.rollback()

    for plan in (first_plan, cursor_plan):
        nodes = list(_walk_plan(plan))
        assert any(node.get("Node Type") == "Limit" for node in nodes)


def _explain_json(connection, statement, engine: Engine) -> dict[str, object]:
    compiled = statement.compile(
        dialect=engine.dialect,
        compile_kwargs={"literal_binds": True},
    )
    payload = connection.execute(
        text(f"EXPLAIN (FORMAT JSON, COSTS OFF) {compiled}")
    ).scalar_one()
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload[0]["Plan"]


def _walk_plan(node: dict[str, object]):
    yield node
    for child in node.get("Plans", []):
        yield from _walk_plan(child)
