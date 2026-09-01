from __future__ import annotations

from datetime import date, datetime
import json

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.place import MemoryKeeperPlace
from app.memorykeeper.repositories.fast_travel_repository import (
    MemoryKeeperFastTravelRepository,
)
from app.memorykeeper.services.fast_travel_service import (
    MemoryKeeperFastTravelService,
)
from scripts.db_migrate import run_stamp_baseline, run_upgrade
from tests.integration.postgresql.support import create_legacy_schema, run_with_engine_patch


pytestmark = pytest.mark.postgresql_integration


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
    captured_at: datetime,
    place: MemoryKeeperPlace,
) -> CommonFile:
    common_file = CommonFile(
        file_id=f"{suffix:064x}",
        original_name=f"{suffix}.jpg",
        preview_path=f"preview/{suffix}.jpg",
        thumb_path=f"thumb/{suffix}.jpg",
        deleted=False,
    )
    session.add(common_file)
    session.flush()
    session.add(CommonFileService(file_id=common_file.id, service_name="MemoryKeeper"))
    session.add(
        CommonFileMetadata(
            file_id=common_file.id,
            memorykeeper_place_id=place.id,
        )
    )
    session.add(
        MemoryKeeperFileState(
            file_id=common_file.id,
            effective_capture_datetime=captured_at,
            date_basis="EXIF",
        )
    )
    session.flush()
    return common_file


def test_fast_travel_uses_generated_dates_for_aggregates_and_memories(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    _upgrade_to_head(postgresql_engine, migration_engine_factory)
    with Session(postgresql_engine) as session:
        place = MemoryKeeperPlace(
            id="11111111-1111-1111-1111-111111111111",
            display_name="도쿄",
            canonical_name="도쿄",
            country="일본",
            city="도쿄도",
            latitude=35.6762,
            longitude=139.6503,
            active=True,
        )
        session.add(place)
        _add_photo(
            session,
            suffix=1,
            captured_at=datetime(2013, 9, 1, 23, 59),
            place=place,
        )
        _add_photo(
            session,
            suffix=2,
            captured_at=datetime(2025, 9, 1, 8, 0),
            place=place,
        )
        _add_photo(
            session,
            suffix=3,
            captured_at=datetime(2025, 9, 2, 8, 0),
            place=place,
        )
        _add_photo(
            session,
            suffix=4,
            captured_at=datetime(2025, 12, 20, 8, 0),
            place=place,
        )
        session.commit()

        service = MemoryKeeperFastTravelService(session)
        aggregates = service.aggregates()
        assert len(aggregates.places) == 1
        assert aggregates.places[0].photo_count == 4
        assert aggregates.places[0].capture_dates == [
            date(2013, 9, 1),
            date(2025, 9, 1),
            date(2025, 9, 2),
            date(2025, 12, 20),
        ]
        assert aggregates.places[0].visit_count == 3
        assert aggregates.countries[0].photo_count == 4
        assert aggregates.countries[0].visit_count == 3

        memories = service.memories(reference_date=date(2026, 9, 1), limit=10)
        assert [item.effective_capture_date for item in memories.exact_anniversary] == [
            date(2025, 9, 1),
            date(2013, 9, 1),
        ]
        assert [item.effective_capture_date for item in memories.previous_year_period] == [
            date(2025, 9, 2)
        ]


def test_fast_travel_queries_remain_set_based_at_current_catalog_scale(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    _upgrade_to_head(postgresql_engine, migration_engine_factory)
    with postgresql_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO common_files (
                    file_id, original_name, service_name, preview_path,
                    thumb_path, deleted
                )
                SELECT
                    lpad(to_hex(value), 64, '0'),
                    value::text || '.jpg',
                    'MemoryKeeper',
                    'preview/' || value::text || '.jpg',
                    'thumb/' || value::text || '.jpg',
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
                INSERT INTO common_file_metadata (
                    file_id, country, city, place_name
                )
                SELECT
                    id,
                    CASE WHEN id % 2 = 0 THEN '대한민국' ELSE '일본' END,
                    CASE WHEN id % 3 = 0 THEN '서울' ELSE '도쿄' END,
                    'place-' || (id % 20)::text
                FROM common_files
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO memorykeeper_file_states (
                    file_id, effective_capture_datetime, date_basis
                )
                SELECT
                    id,
                    TIMESTAMP '2013-01-01 12:00:00'
                        + (id % 4500) * INTERVAL '1 day',
                    'EXIF'
                FROM common_files
                """
            )
        )
        for table_name in (
            "common_files",
            "common_file_services",
            "common_file_metadata",
            "memorykeeper_file_states",
        ):
            connection.execute(text(f"ANALYZE {table_name}"))

    with Session(postgresql_engine) as session:
        repository = MemoryKeeperFastTravelRepository(session)
        statements = [
            repository.build_place_date_statement(),
            repository.build_country_date_statement(),
            repository.build_exact_anniversary_statement(date(2026, 9, 1)),
            repository.build_previous_year_period_statement(
                date_from=date(2025, 8, 25),
                date_to=date(2025, 9, 8),
                reference_date=date(2026, 9, 1),
            ),
        ]

    with postgresql_engine.connect() as connection:
        plans = [
            _explain_analyze_json(connection, statement, postgresql_engine)
            for statement in statements
        ]
        connection.rollback()

    # The endpoint budgets are server execution time, not network wall time.
    aggregate_ms = sum(float(plan["Execution Time"]) for plan in plans[:2])
    memory_ms = sum(float(plan["Execution Time"]) for plan in plans[2:])
    assert aggregate_ms <= 300.0
    assert memory_ms <= 200.0


def _explain_analyze_json(connection, statement, engine: Engine) -> dict[str, object]:
    compiled = statement.compile(
        dialect=engine.dialect,
        compile_kwargs={"literal_binds": True},
    )
    payload = connection.execute(
        text(
            "EXPLAIN (ANALYZE, FORMAT JSON, COSTS OFF, BUFFERS OFF, TIMING OFF) "
            f"{compiled}"
        )
    ).scalar_one()
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload[0]

