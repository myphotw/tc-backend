from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.common.model_registry import Base
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.place import MemoryKeeperPlace
from app.memorykeeper.services.fast_travel_service import (
    MemoryKeeperFastTravelService,
)


class TestMemoryKeeperFastTravel:
    def setup_method(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.service = MemoryKeeperFastTravelService(self.db)
        self.counter = 0

    def teardown_method(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _place(
        self,
        *,
        name: str,
        country: str | None,
        city: str | None,
    ) -> MemoryKeeperPlace:
        place = MemoryKeeperPlace(
            id=str(uuid4()),
            display_name=name,
            canonical_name=name,
            country=country,
            city=city,
            latitude=35.0,
            longitude=135.0,
            active=True,
        )
        self.db.add(place)
        self.db.flush()
        return place

    def _photo(
        self,
        captured_date: date | None,
        *,
        captured_at: datetime | None = None,
        service_name: str = "MemoryKeeper",
        deleted: bool = False,
        place: MemoryKeeperPlace | None = None,
        raw_place_name: str | None = None,
        raw_country: str | None = None,
        raw_city: str | None = None,
        preview: bool = True,
        thumbnail: bool = True,
    ) -> CommonFile:
        self.counter += 1
        public_id = f"{self.counter:064x}"
        effective_at = captured_at
        if effective_at is None and captured_date is not None:
            effective_at = datetime.combine(captured_date, datetime.min.time())
        common_file = CommonFile(
            file_id=public_id,
            original_name=f"{self.counter}.jpg",
            preview_path=f"preview/{public_id}.jpg" if preview else None,
            thumb_path=f"thumb/{public_id}.jpg" if thumbnail else None,
            deleted=deleted,
            service_name=service_name,
        )
        self.db.add(common_file)
        self.db.flush()
        self.db.add(
            CommonFileService(file_id=common_file.id, service_name=service_name)
        )
        self.db.add(
            CommonFileMetadata(
                file_id=common_file.id,
                memorykeeper_place_id=place.id if place else None,
                place_name=raw_place_name,
                country=raw_country,
                city=raw_city,
            )
        )
        self.db.add(
            MemoryKeeperFileState(
                file_id=common_file.id,
                effective_capture_datetime=effective_at,
                effective_capture_date=captured_date,
                effective_capture_year=(captured_date.year if captured_date else None),
                date_basis="EXIF" if captured_date else None,
            )
        )
        self.db.commit()
        return common_file

    def test_empty_aggregates_are_normal_and_set_based(self) -> None:
        statements: list[str] = []

        def capture(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            response = self.service.aggregates()
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)

        assert response.places == []
        assert response.countries == []
        assert len(statements) == 2

    def test_place_and_country_dates_counts_visits_and_representatives(self) -> None:
        tokyo = self._place(name="도쿄", country="일본", city="도쿄도")
        osaka = self._place(name="오사카", country="일본", city="오사카부")

        oldest_media = self._photo(date(2025, 1, 1), place=tokyo)
        self._photo(
            date(2025, 1, 1),
            captured_at=datetime(2025, 1, 1, 23, 30),
            place=tokyo,
            preview=False,
            thumbnail=False,
        )
        self._photo(date(2025, 1, 2), place=tokyo)
        latest_tokyo = self._photo(date(2025, 4, 10), place=tokyo)
        latest_tokyo_media = self._photo(date(2025, 4, 11), place=tokyo)
        self._photo(date(2025, 1, 1), place=osaka)
        latest_japan = self._photo(date(2026, 2, 1), place=osaka)

        response = self.service.aggregates()

        assert len(response.places) == 2
        tokyo_result = next(
            item for item in response.places if item.memorykeeper_place_id == tokyo.id
        )
        assert tokyo_result.photo_count == 5
        assert tokyo_result.capture_dates == [
            date(2025, 1, 1),
            date(2025, 1, 2),
            date(2025, 4, 10),
            date(2025, 4, 11),
        ]
        assert tokyo_result.visit_count == 2
        assert tokyo_result.representative_common_file_id != oldest_media.id
        assert tokyo_result.representative_common_file_id == latest_tokyo_media.id
        assert tokyo_result.representative_capture_date == date(2025, 4, 11)

        japan = next(item for item in response.countries if item.country == "일본")
        assert japan.photo_count == 7
        assert japan.capture_dates == [
            date(2025, 1, 1),
            date(2025, 1, 2),
            date(2025, 4, 10),
            date(2025, 4, 11),
            date(2026, 2, 1),
        ]
        assert japan.visit_count == 3
        assert japan.representative_common_file_id == latest_japan.id
        assert latest_tokyo.id != latest_japan.id

    def test_unknowns_are_preserved_and_ineligible_files_are_excluded(self) -> None:
        included = self._photo(
            date(2024, 7, 1),
            raw_place_name=None,
            raw_country=None,
            raw_city=None,
        )
        self._photo(date(2024, 7, 2), deleted=True)
        self._photo(date(2024, 7, 3), service_name="AstroJournal")
        self._photo(None)

        response = self.service.aggregates()

        assert len(response.places) == 1
        assert response.places[0].memorykeeper_place_id is None
        assert response.places[0].place_display_name is None
        assert response.places[0].country is None
        assert response.places[0].region is None
        assert response.places[0].photo_count == 1
        assert response.places[0].representative_common_file_id == included.id
        assert len(response.countries) == 1
        assert response.countries[0].country is None

    def test_effective_capture_date_is_not_recomputed_from_datetime(self) -> None:
        place = self._place(name="경계", country="일본", city="도쿄도")
        self._photo(
            date(2025, 1, 1),
            captured_at=datetime(2025, 1, 2, 0, 30),
            place=place,
        )

        response = self.service.aggregates()

        assert response.places[0].capture_dates == [date(2025, 1, 1)]

    def test_memories_return_exact_years_and_one_deterministic_photo_per_date(self) -> None:
        place = self._place(name="교토", country="일본", city="교토부")
        older_without_media = self._photo(
            date(2013, 9, 1),
            captured_at=datetime(2013, 9, 1, 23, 59),
            place=place,
            preview=False,
            thumbnail=False,
        )
        exact_2013 = self._photo(
            date(2013, 9, 1),
            captured_at=datetime(2013, 9, 1, 8, 0),
            place=place,
        )
        exact_2025 = self._photo(date(2025, 9, 1), place=place)
        nearby = self._photo(date(2025, 8, 30), place=place, thumbnail=False)
        self._photo(date(2026, 9, 1), place=place)
        self._photo(date(2027, 9, 1), place=place)

        statements: list[str] = []

        def capture(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            response = self.service.memories(
                reference_date=date(2026, 9, 1),
                limit=10,
            )
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)

        assert [item.effective_capture_date for item in response.exact_anniversary] == [
            date(2025, 9, 1),
            date(2013, 9, 1),
        ]
        assert response.exact_anniversary[1].common_file_id == exact_2013.id
        assert response.exact_anniversary[1].common_file_id != older_without_media.id
        assert all(item.day_offset == 0 for item in response.exact_anniversary)
        assert [item.common_file_id for item in response.previous_year_period] == [
            nearby.id
        ]
        assert response.previous_year_period[0].day_offset == -2
        assert response.previous_year_period[0].thumbnail_url is None
        assert response.previous_year_period[0].preview_url is not None
        assert response.exact_anniversary[0].years_ago == 1
        assert response.exact_anniversary[0].common_file_id == exact_2025.id
        assert len(statements) == 2

    def test_memories_without_exact_use_nearest_previous_year_and_respect_limit(self) -> None:
        place = self._place(name="부산", country="대한민국", city="부산")
        near_later = self._photo(date(2025, 9, 3), place=place)
        near_earlier = self._photo(date(2025, 8, 31), place=place)
        self._photo(date(2025, 8, 25), place=place)
        self._photo(date(2025, 9, 9), place=place)

        response = self.service.memories(
            reference_date=date(2026, 9, 1),
            limit=2,
        )

        assert response.exact_anniversary == []
        assert [item.common_file_id for item in response.previous_year_period] == [
            near_earlier.id,
            near_later.id,
        ]

    def test_routes_are_additive(self) -> None:
        from app.main import app

        def route_paths(routes):
            for route in routes:
                if hasattr(route, "path"):
                    yield route.path
                nested = getattr(route, "routes", None)
                if nested is None:
                    original_router = getattr(route, "original_router", None)
                    nested = getattr(original_router, "routes", None)
                if nested is not None:
                    yield from route_paths(nested)

        paths = set(route_paths(app.routes))
        assert "/api/memorykeeper/travel/aggregates" in paths
        assert "/api/memorykeeper/travel/memories" in paths
        assert "/api/memorykeeper/gallery/photos" in paths
        assert "/api/common/gallery/search" in paths
