from __future__ import annotations

import base64
from datetime import date, datetime, timedelta
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from app.common.model_registry import Base
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.place import MemoryKeeperPlace
from app.memorykeeper.repositories.fast_gallery_repository import (
    FastGalleryFilters,
    MemoryKeeperFastGalleryRepository,
)
from app.memorykeeper.services.fast_gallery_service import MemoryKeeperFastGalleryService
from app.memorykeeper.services.fast_gallery_cursor import decode_cursor
from app.memorykeeper.services.fast_gallery_location import (
    decode_location_key,
    encode_raw_location_key,
    encode_registered_location_key,
)


class TestMemoryKeeperFastGallery:
    def setup_method(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.service = MemoryKeeperFastGalleryService(self.db)
        self.counter = 0

    def teardown_method(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _place(
        self,
        *,
        display_name: str = "서울숲",
        country: str | None = "대한민국",
        province: str | None = None,
        city: str | None = "서울",
    ) -> MemoryKeeperPlace:
        place = MemoryKeeperPlace(
            id=str(uuid4()),
            display_name=display_name,
            canonical_name=display_name,
            country=country,
            province=province,
            city=city,
            latitude=37.5,
            longitude=127.0,
            active=True,
        )
        self.db.add(place)
        self.db.flush()
        return place

    def _photo(
        self,
        captured_at: datetime | None,
        *,
        service_name: str = "MemoryKeeper",
        deleted: bool = False,
        favorite: bool = False,
        gps: bool = False,
        place: MemoryKeeperPlace | None = None,
        country: str | None = "대한민국",
        province: str | None = None,
        city: str | None = "서울",
        place_name: str | None = "원시 장소",
        date_basis: str | None = "EXIF",
        source_capture_year: int | None = None,
        source_capture_year_basis: str | None = None,
        effective_capture_precision: str | None = None,
        photo_category: str = "NORMAL",
        category_revision: int = 0,
        preview: bool = True,
        thumbnail: bool = True,
    ) -> CommonFile:
        self.counter += 1
        public_id = f"{self.counter:064x}"
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
                gps_lat=37.5 if gps else None,
                gps_lon=127.0 if gps else None,
                country=country,
                province=province,
                city=city,
                place_name=place_name,
                memorykeeper_place_id=place.id if place else None,
            )
        )
        # SQLite's Base.metadata.create_all() deliberately has no PostgreSQL
        # generated-column semantics, so tests populate those two projections
        # explicitly. Production obtains them from revision 20260901_0002.
        self.db.add(
            MemoryKeeperFileState(
                file_id=common_file.id,
                favorite=favorite,
                effective_capture_datetime=captured_at,
                effective_capture_date=(captured_at.date() if captured_at else None),
                effective_capture_year=(
                    captured_at.year if captured_at else source_capture_year
                ),
                effective_capture_precision=(
                    effective_capture_precision
                    or ("DATETIME" if captured_at is not None else None)
                ),
                date_basis=(
                    date_basis
                    if captured_at is not None or source_capture_year is not None
                    else None
                ),
                source_capture_year=source_capture_year,
                source_capture_year_basis=source_capture_year_basis,
                photo_category=photo_category,
                photo_category_revision=category_revision,
            )
        )
        self.db.commit()
        return common_file

    def test_keyset_is_deterministic_with_duplicate_timestamps_and_no_count(self) -> None:
        timestamp = datetime(2024, 5, 3, 12, 0, 0)
        first = self._photo(timestamp)
        second = self._photo(timestamp)
        third = self._photo(timestamp)
        fourth = self._photo(datetime(2024, 5, 2, 12, 0, 0))
        statements: list[str] = []

        def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            page_one = self.service.photos(
                cursor=None,
                limit=2,
                filters=FastGalleryFilters(),
            )
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)

        assert page_one.has_more is True
        assert page_one.next_cursor is not None
        assert [item.common_file_id for item in page_one.items] == [third.id, second.id]
        assert len(statements) == 1
        assert "count(" not in statements[0].casefold()

        page_two = self.service.photos(
            cursor=page_one.next_cursor,
            limit=2,
            filters=FastGalleryFilters(),
        )
        assert page_two.has_more is False
        assert page_two.next_cursor is None
        assert [item.common_file_id for item in page_two.items] == [first.id, fourth.id]
        assert {
            item.common_file_id for item in [*page_one.items, *page_two.items]
        } == {first.id, second.id, third.id, fourth.id}
        assert page_one.sync_cursor is None

    def test_date_unclassified_is_year_scoped_and_file_id_paginated(self) -> None:
        place = self._place(display_name="등록 장소")
        first = self._photo(
            None,
            source_capture_year=2023,
            source_capture_year_basis="ORIGINAL_PATH",
            effective_capture_precision="YEAR",
            date_basis="SOURCE_YEAR",
            place=place,
        )
        second = self._photo(
            None,
            source_capture_year=2023,
            source_capture_year_basis="ORIGINAL_PATH",
            effective_capture_precision="YEAR",
            date_basis="SOURCE_YEAR",
            country=None,
            city=None,
            place_name=None,
        )
        daily = self._photo(
            None,
            source_capture_year=2023,
            source_capture_year_basis="ORIGINAL_PATH",
            effective_capture_precision="YEAR",
            date_basis="SOURCE_YEAR",
            photo_category="DAILY",
        )
        exact = self._photo(datetime(2023, 5, 5, 12, 0))
        self._photo(
            None,
            source_capture_year=2024,
            source_capture_year_basis="ORIGINAL_PATH",
            effective_capture_precision="YEAR",
            date_basis="SOURCE_YEAR",
        )

        page_one = self.service.photos(
            cursor=None,
            limit=2,
            filters=FastGalleryFilters(year=2023, date_unclassified=True),
        )
        page_two = self.service.photos(
            cursor=page_one.next_cursor,
            limit=2,
            filters=FastGalleryFilters(year=2023, date_unclassified=True),
        )

        assert page_one.has_more is True
        assert page_one.next_cursor is not None
        assert page_two.has_more is False
        assert {
            item.common_file_id for item in [*page_one.items, *page_two.items]
        } == {first.id, second.id, daily.id}
        assert exact.id not in {
            item.common_file_id for item in [*page_one.items, *page_two.items]
        }
        assert all(item.effective_capture_datetime is None for item in page_one.items)
        assert all(item.effective_capture_date is None for item in page_one.items)
        assert all(item.effective_capture_year == 2023 for item in page_one.items)
        assert all(item.effective_capture_precision == "YEAR" for item in page_one.items)
        assert all(item.date_basis == "SOURCE_YEAR" for item in page_one.items)

        hierarchy = self.service.hierarchy()
        year = next(item for item in hierarchy.items if item.year == 2023)
        assert year.count == 4
        assert year.date_unclassified_count == 3
        assert year.daily_count == 1
        # Place classification is independent from date precision. Only the
        # row missing canonical country/region/place is location-unclassified.
        assert year.unclassified_count == 1

    def test_date_unclassified_requires_year_and_keeps_place_filter_separate(
        self,
    ) -> None:
        with pytest.raises(HTTPException) as missing_year:
            self.service.photos(
                cursor=None,
                limit=50,
                filters=FastGalleryFilters(date_unclassified=True),
            )
        assert missing_year.value.status_code == 400
        assert (
            missing_year.value.detail["code"]
            == "YEAR_REQUIRED_FOR_DATE_UNCLASSIFIED"
        )

        place = self._place(display_name="등록 장소")
        registered = self._photo(
            None,
            source_capture_year=2023,
            source_capture_year_basis="ORIGINAL_PATH",
            effective_capture_precision="YEAR",
            date_basis="SOURCE_YEAR",
            place=place,
        )
        unregistered = self._photo(
            None,
            source_capture_year=2023,
            source_capture_year_basis="ORIGINAL_PATH",
            effective_capture_precision="YEAR",
            date_basis="SOURCE_YEAR",
            country=None,
            city=None,
            place_name=None,
        )

        all_year_only = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2023, date_unclassified=True),
        )
        place_unclassified = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(
                year=2023,
                date_unclassified=True,
                unclassified=True,
            ),
        )

        assert {item.common_file_id for item in all_year_only.items} == {
            registered.id,
            unregistered.id,
        }
        assert [item.common_file_id for item in place_unclassified.items] == [
            unregistered.id
        ]

        ordinary_place_unclassified = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2023, unclassified=True),
        )
        assert [
            item.common_file_id for item in ordinary_place_unclassified.items
        ] == [unregistered.id]

    def test_year_only_participates_in_general_gallery_filters(self) -> None:
        place = self._place(display_name="등록 장소")
        registered = self._photo(
            None,
            source_capture_year=2025,
            source_capture_year_basis="ORIGINAL_PATH",
            effective_capture_precision="YEAR",
            date_basis="SOURCE_YEAR",
            place=place,
            favorite=True,
            gps=True,
        )
        unclassified = self._photo(
            None,
            source_capture_year=2025,
            source_capture_year_basis="ORIGINAL_PATH",
            effective_capture_precision="YEAR",
            date_basis="SOURCE_YEAR",
            country=None,
            city=None,
            place_name=None,
        )
        self._photo(
            None,
            source_capture_year=2024,
            source_capture_year_basis="ORIGINAL_PATH",
            effective_capture_precision="YEAR",
            date_basis="SOURCE_YEAR",
        )

        by_year = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2025),
        )
        favorites = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2025, favorite=True),
        )
        gps = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2025, has_gps=True),
        )
        registered_place = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2025, place_id=place.id),
        )
        country = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2025, country="대한민국"),
        )
        region = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(
                year=2025,
                country="대한민국",
                region="서울",
            ),
        )
        normal = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2025, photo_category="NORMAL"),
        )

        assert {item.common_file_id for item in by_year.items} == {
            registered.id,
            unclassified.id,
        }
        assert [item.common_file_id for item in favorites.items] == [registered.id]
        assert [item.common_file_id for item in gps.items] == [registered.id]
        assert [item.common_file_id for item in registered_place.items] == [
            registered.id
        ]
        assert [item.common_file_id for item in country.items] == [registered.id]
        assert [item.common_file_id for item in region.items] == [registered.id]
        assert {item.common_file_id for item in normal.items} == {
            registered.id,
            unclassified.id,
        }

    def test_mixed_precision_keyset_is_stable_across_year_and_null_boundaries(
        self,
    ) -> None:
        exact_2025 = self._photo(datetime(2025, 6, 1, 12, 0))
        year_only_2025_old = self._photo(
            None,
            source_capture_year=2025,
            source_capture_year_basis="ORIGINAL_PATH",
            effective_capture_precision="YEAR",
            date_basis="SOURCE_YEAR",
        )
        year_only_2025_new = self._photo(
            None,
            source_capture_year=2025,
            source_capture_year_basis="ORIGINAL_PATH",
            effective_capture_precision="YEAR",
            date_basis="SOURCE_YEAR",
        )
        exact_2024 = self._photo(datetime(2024, 12, 31, 23, 59))
        year_only_2024 = self._photo(
            None,
            source_capture_year=2024,
            source_capture_year_basis="ORIGINAL_PATH",
            effective_capture_precision="YEAR",
            date_basis="SOURCE_YEAR",
        )

        pages = []
        cursor = None
        while True:
            page = self.service.photos(
                cursor=cursor,
                limit=2,
                filters=FastGalleryFilters(),
            )
            pages.extend(page.items)
            if not page.has_more:
                break
            assert page.next_cursor is not None
            cursor = page.next_cursor

        delivered = [item.common_file_id for item in pages]
        assert delivered == [
            exact_2025.id,
            year_only_2025_new.id,
            year_only_2025_old.id,
            exact_2024.id,
            year_only_2024.id,
        ]
        assert len(delivered) == len(set(delivered))

    def test_date_range_does_not_assign_a_synthetic_date_to_year_only(self) -> None:
        exact = self._photo(datetime(2025, 5, 3, 12, 0))
        self._photo(
            None,
            source_capture_year=2025,
            source_capture_year_basis="ORIGINAL_PATH",
            effective_capture_precision="YEAR",
            date_basis="SOURCE_YEAR",
        )

        response = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(
                year=2025,
                date_from=date(2025, 1, 1),
                date_to=date(2025, 12, 31),
            ),
        )

        assert [item.common_file_id for item in response.items] == [exact.id]

    def test_legacy_exact_date_cursor_is_accepted_during_cursor_rollout(self) -> None:
        payload = {
            "v": 1,
            "effective_capture_datetime": "2025-05-03T12:00:00.000000",
            "file_id": 123,
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).decode("ascii").rstrip("=")

        cursor = decode_cursor(encoded)

        assert cursor.effective_capture_year == 2025
        assert cursor.effective_capture_datetime == datetime(2025, 5, 3, 12, 0)
        assert cursor.file_id == 123

    def test_filters_and_null_deleted_and_astro_rows_are_excluded(self) -> None:
        place = self._place()
        matched = self._photo(
            datetime(2025, 1, 2, 10, 0),
            favorite=True,
            gps=True,
            place=place,
            country="raw-country-is-not-canonical",
        )
        self._photo(datetime(2024, 1, 2, 10, 0), favorite=True, gps=True)
        self._photo(datetime(2025, 1, 2, 10, 0), favorite=False, gps=True)
        self._photo(datetime(2025, 1, 2, 10, 0), favorite=True, gps=False)
        self._photo(None)
        self._photo(datetime(2025, 1, 2, 10, 0), deleted=True)
        self._photo(datetime(2025, 1, 2, 10, 0), service_name="AstroJournal")

        response = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(
                year=2025,
                country="대한민국",
                region="서울",
                place_id=place.id,
                favorite=True,
                has_gps=True,
                date_from=date(2025, 1, 2),
                date_to=date(2025, 1, 2),
            ),
        )

        assert [item.common_file_id for item in response.items] == [matched.id]
        item = response.items[0]
        assert item.place_display_name == "서울숲"
        assert item.preview_url == f"/api/common/gallery/{matched.file_id}/preview"
        assert item.thumbnail_url == f"/api/common/gallery/{matched.file_id}/thumbnail"

    def test_unclassified_uses_canonical_hierarchy_and_matches_hierarchy_count(
        self,
    ) -> None:
        incomplete_place = self._place(
            display_name="등록 장소",
            country=None,
            city=None,
        )
        incomplete_registered = self._photo(
            datetime(2025, 1, 3, 10, 0),
            place=incomplete_place,
            country=None,
            city=None,
            place_name=None,
        )
        complete_raw_location = self._photo(
            datetime(2025, 1, 2, 10, 0),
            country="대한민국",
            city="강릉",
            place_name="원시 장소",
        )
        self._photo(datetime(2024, 1, 1, 10, 0))

        response = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2025, unclassified=True),
        )

        assert [item.common_file_id for item in response.items] == [
            incomplete_registered.id
        ]
        assert complete_raw_location.id not in {
            item.common_file_id for item in response.items
        }
        assert set(response.model_dump()) == {
            "items",
            "next_cursor",
            "has_more",
            "sync_cursor",
        }

        hierarchy = self.service.hierarchy()
        year = next(item for item in hierarchy.items if item.year == 2025)
        leaves = [
            leaf
            for country in year.countries
            for region in country.regions
            for leaf in region.places
        ]
        unclassified_leaf = next(
            leaf
            for leaf in leaves
            if leaf.memorykeeper_place_id is None
            and decode_location_key(leaf.location_key)
            == decode_location_key(
                encode_raw_location_key(country=None, region=None, place=None)
            )
        )
        assert unclassified_leaf.count == len(response.items) == 1
        assert unclassified_leaf.display_name is None
        assert decode_location_key(unclassified_leaf.location_key) == (
            decode_location_key(
                encode_raw_location_key(country=None, region=None, place=None)
            )
        )

    def test_operational_unclassified_count_includes_incomplete_registered_place(
        self,
    ) -> None:
        true_unclassified = [
            self._photo(
                None,
                source_capture_year=2025,
                source_capture_year_basis="ORIGINAL_PATH",
                effective_capture_precision="YEAR",
                date_basis="SOURCE_YEAR",
                country=None,
                city=None,
                place_name=None,
            )
            for _ in range(22)
        ]
        incomplete_place = self._place(
            display_name="계층 미완성 등록 장소",
            country=None,
            province=None,
            city=None,
        )
        incomplete_registered = self._photo(
            datetime(2025, 7, 1, 12, 0),
            place=incomplete_place,
            country=None,
            province=None,
            city=None,
            place_name=None,
        )
        fallback_place = self._place(
            display_name="fallback 대상",
            country=None,
            province=None,
            city=None,
        )
        fallback_complete = self._photo(
            datetime(2025, 6, 1, 12, 0),
            place=fallback_place,
            country="대한민국",
            city="인천",
            place_name="메타데이터 장소",
        )
        daily = self._photo(
            datetime(2025, 5, 1, 12, 0),
            country=None,
            city=None,
            place_name=None,
            photo_category="DAILY",
        )

        unclassified = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2025, unclassified=True),
        )
        date_unclassified = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2025, date_unclassified=True),
        )
        both = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(
                year=2025,
                unclassified=True,
                date_unclassified=True,
            ),
        )
        hierarchy = self.service.hierarchy()
        summary = self.service.summary()
        year = next(item for item in hierarchy.items if item.year == 2025)
        visible_unclassified = next(
            country for country in year.countries if country.country is None
        )

        expected_unclassified_ids = {
            *(photo.id for photo in true_unclassified),
            incomplete_registered.id,
        }
        assert {item.common_file_id for item in unclassified.items} == (
            expected_unclassified_ids
        )
        assert len(unclassified.items) == 23
        assert year.unclassified_count == 23
        assert visible_unclassified.count == 23
        assert summary.pending_count == 23
        assert {item.common_file_id for item in date_unclassified.items} == {
            photo.id for photo in true_unclassified
        }
        assert {item.common_file_id for item in both.items} == {
            photo.id for photo in true_unclassified
        }
        assert year.date_unclassified_count == 22
        assert year.daily_count == 1
        assert fallback_complete.id not in expected_unclassified_ids
        assert daily.id not in expected_unclassified_ids

    def test_location_hierarchy_completeness_matrix(self) -> None:
        missing_country_and_region = self._place(
            display_name="국가 지역 없음",
            country=None,
            province=None,
            city=None,
        )
        missing_region = self._place(
            display_name="지역 없음",
            country="대한민국",
            province=None,
            city=None,
        )
        city_without_province = self._place(
            display_name="시 단위 장소",
            country="대한민국",
            province=None,
            city="서울",
        )
        complete = self._place(
            display_name="완전한 장소",
            country="대한민국",
            province="경기도",
            city="성남",
        )
        fallback_place = self._place(
            display_name="fallback 장소",
            country=None,
            province=None,
            city=None,
        )

        missing_country_photo = self._photo(
            datetime(2025, 4, 5, 12, 0),
            place=missing_country_and_region,
            country=None,
            province=None,
            city=None,
            place_name=None,
        )
        missing_region_photo = self._photo(
            datetime(2025, 4, 4, 12, 0),
            place=missing_region,
            country=None,
            province=None,
            city=None,
            place_name=None,
        )
        missing_place_photo = self._photo(
            datetime(2025, 4, 3, 12, 0),
            country="대한민국",
            province="강원도",
            city=None,
            place_name="   ",
        )
        city_without_province_photo = self._photo(
            datetime(2025, 4, 2, 12, 0),
            place=city_without_province,
            country=None,
            province=None,
            city=None,
            place_name=None,
        )
        complete_photo = self._photo(
            datetime(2025, 4, 1, 12, 0),
            place=complete,
            country=None,
            province=None,
            city=None,
            place_name=None,
        )
        fallback_complete_photo = self._photo(
            datetime(2025, 3, 31, 12, 0),
            place=fallback_place,
            country="대한민국",
            province="제주특별자치도",
            city=None,
            place_name="fallback 이름",
        )

        response = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2025, unclassified=True),
        )

        assert {item.common_file_id for item in response.items} == {
            missing_country_photo.id,
            missing_region_photo.id,
            missing_place_photo.id,
        }
        assert city_without_province_photo.id not in {
            item.common_file_id for item in response.items
        }
        assert complete_photo.id not in {
            item.common_file_id for item in response.items
        }
        assert fallback_complete_photo.id not in {
            item.common_file_id for item in response.items
        }

    def test_unclassified_preserves_keyset_pagination_and_raw_refinement(self) -> None:
        expected = [
            self._photo(
                datetime(2025, 1, day, 10, 0),
                country="대한민국",
                city="강릉",
                place_name=None,
            )
            for day in (5, 3, 1)
        ]
        registered_place = self._place()
        self._photo(datetime(2025, 1, 4, 10, 0), place=registered_place)

        first = self.service.photos(
            cursor=None,
            limit=2,
            filters=FastGalleryFilters(year=2025, unclassified=True),
        )
        second = self.service.photos(
            cursor=first.next_cursor,
            limit=2,
            filters=FastGalleryFilters(year=2025, unclassified=True),
        )
        refined = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(
                year=2025,
                country="대한민국",
                region="강릉",
                unclassified=True,
            ),
        )

        delivered = [item.common_file_id for item in [*first.items, *second.items]]
        assert delivered == [photo.id for photo in expected]
        assert len(delivered) == len(set(delivered))
        assert first.has_more is True
        assert second.has_more is False
        assert [item.common_file_id for item in refined.items] == delivered
        hierarchy = self.service.hierarchy()
        year = next(item for item in hierarchy.items if item.year == 2025)
        unclassified_count = next(
            leaf.count
            for country in year.countries
            for region in country.regions
            for leaf in region.places
            if leaf.memorykeeper_place_id is None
        )
        assert unclassified_count == len(delivered)

    def test_unclassified_rejects_place_and_location_key_combinations(self) -> None:
        place = self._place()
        for filters, location_key in (
            (FastGalleryFilters(unclassified=True, place_id=place.id), None),
            (
                FastGalleryFilters(unclassified=True),
                encode_raw_location_key(
                    country=None,
                    region=None,
                    place=None,
                ),
            ),
        ):
            with pytest.raises(HTTPException) as conflict:
                self.service.photos(
                    cursor=None,
                    limit=50,
                    filters=filters,
                    location_key=location_key,
                )
            assert conflict.value.status_code == 422
            assert (
                conflict.value.detail["code"]
                == "GALLERY_UNCLASSIFIED_FILTER_CONFLICT"
            )

    def test_media_urls_follow_persisted_derivative_paths_without_filesystem_io(
        self,
    ) -> None:
        complete = self._photo(datetime(2026, 1, 3, 10, 0))
        preview_only = self._photo(
            datetime(2026, 1, 2, 10, 0),
            thumbnail=False,
        )
        missing = self._photo(
            datetime(2026, 1, 1, 10, 0),
            preview=False,
            thumbnail=False,
        )

        response = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2026),
        )
        by_id = {item.common_file_id: item for item in response.items}

        assert len(by_id[complete.id].file_id) == 64
        assert by_id[complete.id].thumbnail_url is not None
        assert by_id[complete.id].preview_url is not None
        assert by_id[preview_only.id].thumbnail_url is None
        assert by_id[preview_only.id].preview_url == (
            f"/api/common/gallery/{preview_only.file_id}/preview"
        )
        assert by_id[missing.id].thumbnail_url is None
        assert by_id[missing.id].preview_url is None

    @pytest.mark.parametrize(
        ("extension", "mime_type"),
        [
            (".jpg", "image/jpeg"),
            (".png", "image/png"),
            (".mp4", "video/mp4"),
            (".mov", "video/quicktime"),
            (None, None),
        ],
    )
    def test_photo_items_preserve_optional_media_fields(
        self, extension: str | None, mime_type: str | None,
    ) -> None:
        common_file = self._photo(datetime(2026, 1, 3, 10, 0))
        common_file.extension = extension
        common_file.mime_type = mime_type
        self.db.commit()

        response = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(),
        )
        item = response.items[0]
        payload = item.model_dump(mode="json")
        assert item.common_file_id == common_file.id
        assert payload["extension"] == extension
        assert payload["mime_type"] == mime_type

        # Older payloads remain valid; the new fields are not required.
        payload.pop("extension")
        payload.pop("mime_type")
        legacy_item = type(item).model_validate(payload)
        assert legacy_item.extension is None
        assert legacy_item.mime_type is None

    def test_invalid_cursor_and_invalid_date_range_return_clear_400(self) -> None:
        with pytest.raises(HTTPException) as invalid_cursor:
            self.service.photos(
                cursor="not-a-cursor",
                limit=50,
                filters=FastGalleryFilters(),
            )
        assert invalid_cursor.value.status_code == 400
        assert invalid_cursor.value.detail["code"] == "INVALID_GALLERY_CURSOR"

        with pytest.raises(HTTPException) as invalid_range:
            self.service.photos(
                cursor=None,
                limit=50,
                filters=FastGalleryFilters(
                    date_from=date(2025, 2, 1),
                    date_to=date(2025, 1, 1),
                ),
            )
        assert invalid_range.value.status_code == 400
        assert invalid_range.value.detail["code"] == "INVALID_GALLERY_DATE_RANGE"

    def test_new_rows_do_not_duplicate_keyset_pages(self) -> None:
        newest = self._photo(datetime(2025, 5, 3, 12, 0))
        middle = self._photo(datetime(2025, 5, 2, 12, 0))
        oldest = self._photo(datetime(2025, 5, 1, 12, 0))
        first_page = self.service.photos(
            cursor=None,
            limit=2,
            filters=FastGalleryFilters(),
        )
        assert [item.common_file_id for item in first_page.items] == [newest.id, middle.id]

        # A new row that sorts before the first cursor is intentionally picked
        # up by a later refresh, while an older concurrent row may join the
        # remaining keyset.  Neither case can duplicate an already delivered
        # item.
        newer = self._photo(datetime(2025, 5, 4, 12, 0))
        concurrent_older = self._photo(datetime(2025, 4, 30, 12, 0))
        second_page = self.service.photos(
            cursor=first_page.next_cursor,
            limit=10,
            filters=FastGalleryFilters(),
        )

        assert [item.common_file_id for item in second_page.items] == [
            oldest.id,
            concurrent_older.id,
        ]
        delivered = {
            item.common_file_id for item in [*first_page.items, *second_page.items]
        }
        assert newer.id not in delivered
        assert len(delivered) == len(first_page.items) + len(second_page.items)

    def test_summary_and_hierarchy_are_set_based_and_keep_unknown_nodes(self) -> None:
        place = self._place(display_name="서울숲", country="대한민국", city="서울")
        self._photo(datetime(2025, 3, 1, 8, 0), favorite=True, gps=True, place=place)
        self._photo(datetime(2025, 3, 2, 8, 0), favorite=False, gps=False, place=place)
        self._photo(
            datetime(2024, 4, 1, 8, 0),
            favorite=False,
            gps=False,
            country=None,
            city=None,
        )
        statements: list[str] = []

        def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            summary = self.service.summary()
            hierarchy = self.service.hierarchy()
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)

        assert summary.total_photos == 3
        assert summary.favorite_count == 1
        assert summary.recent_count == 3
        assert summary.pending_count == 1
        assert summary.place_cleanup_count == 1
        assert summary.gps_count == 1
        assert summary.effective_date_min == date(2024, 4, 1)
        assert summary.effective_date_max == date(2025, 3, 2)
        assert [(item.name, item.count) for item in summary.by_year] == [
            ("2025", 2),
            ("2024", 1),
        ]
        assert ("대한민국", 2) in [(item.name, item.count) for item in summary.by_country]
        assert (None, 1) in [(item.name, item.count) for item in summary.by_country]
        assert len(statements) == 5  # four summary queries + one hierarchy GROUP BY
        assert len(hierarchy.items) == 2
        assert hierarchy.items[0].year == 2025
        assert hierarchy.items[0].count == 2
        assert hierarchy.items[0].countries[0].regions[0].places[0].display_name == "서울숲"
        assert hierarchy.items[0].countries[0].regions[0].places[0].location_key == (
            encode_registered_location_key(place.id)
        )
        assert hierarchy.items[1].countries[0].country is None

    def test_daily_is_separate_from_place_and_unclassified_projections(self) -> None:
        place = self._place(display_name="등록 장소", country="대한민국", city="서울")
        registered = self._photo(
            datetime(2025, 6, 3, 8, 0),
            place=place,
            gps=True,
        )
        unclassified = self._photo(
            datetime(2025, 6, 2, 8, 0),
            gps=True,
            country="대한민국",
            city="부산",
            place_name=None,
        )
        daily = self._photo(
            datetime(2025, 6, 1, 8, 0),
            gps=True,
            place=place,
            country="대한민국",
            city="서울",
            photo_category="DAILY",
            category_revision=2,
        )

        daily_page = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(photo_category="DAILY"),
        )
        unclassified_page = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(unclassified=True),
        )
        place_page = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(place_id=place.id),
        )
        summary = self.service.summary()
        hierarchy = self.service.hierarchy()

        assert [item.common_file_id for item in daily_page.items] == [daily.id]
        assert daily_page.items[0].photo_category == "DAILY"
        assert daily_page.items[0].category_revision == 2
        assert daily_page.items[0].memorykeeper_place_id is None
        assert daily_page.items[0].place_display_name is None
        assert daily_page.items[0].country is None
        assert daily_page.items[0].region is None
        assert daily_page.items[0].has_gps is True
        assert [item.common_file_id for item in unclassified_page.items] == [
            unclassified.id
        ]
        assert [item.common_file_id for item in place_page.items] == [registered.id]
        assert summary.total_photos == 3
        assert summary.daily_count == 1
        assert ("대한민국", 2) in [
            (item.name, item.count) for item in summary.by_country
        ]
        year = hierarchy.items[0]
        assert year.year == 2025
        assert year.count == 3
        assert year.daily_count == 1
        assert year.unclassified_count == 1
        assert sum(country.count for country in year.countries) == 2

    def test_daily_rejects_place_hierarchy_filter_combinations(self) -> None:
        for filters, location_key in (
            (FastGalleryFilters(photo_category="DAILY", unclassified=True), None),
            (FastGalleryFilters(photo_category="DAILY", country="대한민국"), None),
            (
                FastGalleryFilters(photo_category="DAILY"),
                encode_raw_location_key(
                    country="대한민국",
                    region="서울",
                    place="원시 장소",
                ),
            ),
        ):
            with pytest.raises(HTTPException) as conflict:
                self.service.photos(
                    cursor=None,
                    limit=50,
                    filters=filters,
                    location_key=location_key,
                )
            assert conflict.value.status_code == 422
            assert conflict.value.detail["code"] == "GALLERY_CATEGORY_FILTER_CONFLICT"

    def test_hierarchy_keeps_complete_raw_location_classified_without_place_id(
        self,
    ) -> None:
        registered = self._place(
            display_name="오리온 호텔",
            country="일본",
            city="Motobu",
        )
        self._photo(
            datetime(2024, 1, 3, 8, 0),
            place=registered,
            country="raw-country",
            city="raw-region",
            place_name="raw-place",
        )
        self._photo(
            datetime(2024, 1, 2, 8, 0),
            country="일본",
            city="Motobu",
            place_name="40 Bise 海辺",
        )

        hierarchy = self.service.hierarchy()
        leaves = [
            place
            for year in hierarchy.items
            for country in year.countries
            for region in country.regions
            for place in region.places
        ]
        registered_leaf = next(
            leaf for leaf in leaves if leaf.memorykeeper_place_id == registered.id
        )
        raw_leaf = next(
            leaf for leaf in leaves if leaf.memorykeeper_place_id is None
        )

        assert registered_leaf.location_key == encode_registered_location_key(
            registered.id
        )
        assert raw_leaf.display_name == "40 Bise 海辺"
        raw_identity = decode_location_key(raw_leaf.location_key)
        assert (raw_identity.country, raw_identity.region, raw_identity.place) == (
            "일본",
            "Motobu",
            "40 Bise 海辺",
        )
        assert hierarchy.items[0].unclassified_count == 0

    def test_raw_location_key_selects_one_leaf_and_excludes_registered_rows(self) -> None:
        registered = self._place(
            display_name="40 Bise 海辺",
            country="일본",
            city="Motobu",
        )
        registered_photo = self._photo(
            datetime(2024, 1, 4, 8, 0),
            place=registered,
            country="일본",
            city="Motobu",
            place_name="40 Bise 海辺",
        )
        bise_new = self._photo(
            datetime(2024, 1, 3, 8, 0),
            country="일본",
            city="Motobu",
            place_name="40 Bise 海辺",
        )
        ishikawa = self._photo(
            datetime(2024, 1, 2, 8, 0),
            country="일본",
            city="Motobu",
            place_name="608 Ishikawa",
        )
        bise_old = self._photo(
            datetime(2024, 1, 1, 8, 0),
            country="일본",
            city="Motobu",
            place_name="40 Bise 海辺",
        )
        key = encode_raw_location_key(
            country="일본",
            region="Motobu",
            place="40 Bise 海辺",
        )

        response = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(
                year=2024,
                country="ignored when location_key is present",
                region="ignored when location_key is present",
            ),
            location_key=key,
        )

        ids = [item.common_file_id for item in response.items]
        assert ids == [bise_new.id, bise_old.id]
        assert registered_photo.id not in ids
        assert ishikawa.id not in ids

    def test_location_key_keeps_registered_place_id_compatibility_explicit(self) -> None:
        place = self._place()
        matched = self._photo(datetime(2025, 1, 2, 10, 0), place=place)
        key = encode_registered_location_key(place.id)

        legacy = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(place_id=place.id),
        )
        keyed = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(place_id=place.id),
            location_key=key,
        )

        assert [item.common_file_id for item in legacy.items] == [matched.id]
        assert [item.common_file_id for item in keyed.items] == [matched.id]

        with pytest.raises(HTTPException) as mismatch:
            self.service.photos(
                cursor=None,
                limit=50,
                filters=FastGalleryFilters(place_id=str(uuid4())),
                location_key=key,
            )
        assert mismatch.value.status_code == 400
        assert mismatch.value.detail["code"] == "GALLERY_LOCATION_FILTER_CONFLICT"

        with pytest.raises(HTTPException) as raw_conflict:
            self.service.photos(
                cursor=None,
                limit=50,
                filters=FastGalleryFilters(place_id=place.id),
                location_key=encode_raw_location_key(
                    country="대한민국",
                    region="서울",
                    place="원시 장소",
                ),
            )
        assert raw_conflict.value.status_code == 400

    def test_non_null_relation_remains_classified_if_place_join_is_unavailable(
        self,
    ) -> None:
        deleted_place = self._place(
            display_name="삭제 장소",
            country="등록 국가",
            city="등록 지역",
        )
        photo = self._photo(
            datetime(2024, 1, 1, 8, 0),
            place=deleted_place,
            country="일본",
            city="Motobu",
            place_name="raw fallback",
        )
        deleted_place.deleted_at = datetime(2024, 1, 2, 8, 0)
        self.db.commit()

        hierarchy = self.service.hierarchy()
        leaves = [
            place
            for year in hierarchy.items
            for country in year.countries
            for region in country.regions
            for place in region.places
        ]

        assert len(leaves) == 1
        assert leaves[0].memorykeeper_place_id == deleted_place.id
        identity = decode_location_key(leaves[0].location_key)
        assert identity.kind == "registered"
        assert identity.place_id == deleted_place.id
        response = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2024),
            location_key=leaves[0].location_key,
        )
        assert [item.common_file_id for item in response.items] == [photo.id]

    def test_raw_location_filter_preserves_keyset_pagination(self) -> None:
        bise = [
            self._photo(
                datetime(2024, 1, day, 8, 0),
                country="일본",
                city="Motobu",
                place_name="40 Bise",
            )
            for day in (5, 3, 1)
        ]
        self._photo(
            datetime(2024, 1, 4, 8, 0),
            country="일본",
            city="Motobu",
            place_name="608 Ishikawa",
        )
        self._photo(
            datetime(2024, 1, 2, 8, 0),
            country="일본",
            city="Motobu",
            place_name="608 Ishikawa",
        )
        key = encode_raw_location_key(
            country="일본", region="Motobu", place="40 Bise"
        )

        page_one = self.service.photos(
            cursor=None,
            limit=2,
            filters=FastGalleryFilters(year=2024),
            location_key=key,
        )
        page_two = self.service.photos(
            cursor=page_one.next_cursor,
            limit=2,
            filters=FastGalleryFilters(year=2024),
            location_key=key,
        )

        delivered = [
            item.common_file_id for item in [*page_one.items, *page_two.items]
        ]
        assert delivered == [photo.id for photo in bise]
        assert len(delivered) == len(set(delivered))
        assert page_one.has_more is True
        assert page_two.has_more is False

    def test_raw_location_filter_preserves_null_and_empty_semantics(self) -> None:
        matched = self._photo(
            datetime(2024, 1, 1, 8, 0),
            country=None,
            city="",
            place_name=None,
        )
        response = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2024),
            location_key=encode_raw_location_key(
                country=None,
                region="",
                place=None,
            ),
        )

        assert [item.common_file_id for item in response.items] == [matched.id]

    def test_all_null_raw_location_key_includes_a_missing_metadata_row(self) -> None:
        missing_metadata = self._photo(
            datetime(2024, 1, 1, 8, 0),
            country=None,
            city=None,
            place_name=None,
        )
        self.db.query(CommonFileMetadata).filter(
            CommonFileMetadata.file_id == missing_metadata.id
        ).delete(synchronize_session=False)
        self.db.commit()
        key = encode_raw_location_key(country=None, region=None, place=None)

        response = self.service.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2024),
            location_key=key,
        )

        assert [item.common_file_id for item in response.items] == [
            missing_metadata.id
        ]

    def test_summary_shortcut_counts_match_legacy_active_file_scope(self) -> None:
        registered_without_country = self._place(country=None, city=None)
        self._photo(
            datetime(2025, 3, 1, 8, 0),
            country="대한민국",
            city="서울",
        )
        self._photo(
            datetime(2025, 3, 2, 8, 0),
            place=registered_without_country,
            country=None,
            city=None,
        )
        # Recent keeps the legacy active-link scope. Pending now matches the
        # canonical Fast Gallery location-unclassified projection.
        self._photo(None)
        self._photo(datetime(2025, 3, 3, 8, 0), deleted=True)
        self._photo(
            datetime(2025, 3, 4, 8, 0),
            service_name="AstroJournal",
        )

        summary = self.service.summary()
        payload = summary.model_dump(mode="json")

        assert summary.total_photos == 2
        assert summary.recent_count == 3
        assert summary.pending_count == 1
        assert summary.place_cleanup_count == 3
        assert payload["favorite_count"] == 0
        assert payload["recent_count"] == 3
        assert payload["pending_count"] == 1
        assert payload["place_cleanup_count"] == 3

    def test_summary_recent_count_is_capped_at_legacy_shortcut_limit(self) -> None:
        for day in range(49):
            self._photo(datetime(2025, 1, 1, 8, 0) + timedelta(days=day))

        summary = self.service.summary()

        assert summary.total_photos == 49
        assert summary.recent_count == 48

    def test_fast_gallery_routes_are_registered_additively(self) -> None:
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
        assert "/api/memorykeeper/gallery/photos" in paths
        assert "/api/memorykeeper/gallery/summary" in paths
        assert "/api/memorykeeper/gallery/hierarchy" in paths
        assert "/api/memorykeeper/place-cleanup" in paths
        assert "/api/common/gallery/search" in paths
        parameters = app.openapi()["paths"]["/api/memorykeeper/gallery/photos"][
            "get"
        ]["parameters"]
        parameter_names = {parameter["name"] for parameter in parameters}
        assert "location_key" in parameter_names
        assert "unclassified" in parameter_names

    def test_postgresql_statement_uses_ordered_candidates_and_lateral_lookups(
        self,
    ) -> None:
        class PostgreSQLBind:
            dialect = postgresql.dialect()

        class StatementOnlySession:
            @staticmethod
            def get_bind():
                return PostgreSQLBind()

        repository = MemoryKeeperFastGalleryRepository(StatementOnlySession())
        statement = repository.build_photos_statement(
            filters=FastGalleryFilters(),
            limit=50,
            cursor_datetime=None,
            cursor_file_id=None,
        )
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

        assert "FROM memorykeeper_file_states" in sql
        assert "LIMIT 51" in sql
        assert "EXISTS (SELECT common_file_services.id" in sql
        assert "JOIN LATERAL" in sql
        assert "common_files.extension" in sql
        assert "common_files.mime_type" in sql
        assert "gallery_file.extension" in sql
        assert "gallery_file.mime_type" in sql
        assert "memorykeeper_file_states.effective_capture_year_v2 IS NOT NULL" in sql
        assert (
            "ORDER BY gallery_candidates.effective_capture_year_v2 DESC, "
            "gallery_candidates.effective_capture_datetime DESC NULLS LAST"
        ) in sql

    def test_postgresql_unclassified_predicate_is_before_candidate_limit(
        self,
    ) -> None:
        class PostgreSQLBind:
            dialect = postgresql.dialect()

        class StatementOnlySession:
            @staticmethod
            def get_bind():
                return PostgreSQLBind()

        repository = MemoryKeeperFastGalleryRepository(StatementOnlySession())
        baseline_statement = repository.build_photos_statement(
            filters=FastGalleryFilters(year=2025),
            limit=50,
            cursor_datetime=None,
            cursor_file_id=None,
        )
        statement = repository.build_photos_statement(
            filters=FastGalleryFilters(year=2025, unclassified=True),
            limit=50,
            cursor_datetime=None,
            cursor_file_id=None,
        )
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        baseline_sql = str(
            baseline_statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

        relation_column = "common_file_metadata.memorykeeper_place_id"
        assert relation_column in sql
        assert "nullif(trim(coalesce(" in sql.casefold()
        assert "NOT (EXISTS" in sql
        assert sql.index(relation_column) < sql.index("LIMIT 51")
        assert baseline_sql.index("LIMIT 51") < baseline_sql.index(relation_column)

    def test_postgresql_raw_location_predicates_are_inside_candidates_before_limit(
        self,
    ) -> None:
        class PostgreSQLBind:
            dialect = postgresql.dialect()

        class StatementOnlySession:
            @staticmethod
            def get_bind():
                return PostgreSQLBind()

        location = decode_location_key(
            encode_raw_location_key(
                country="일본",
                region="Motobu",
                place="40 Bise",
            )
        )
        repository = MemoryKeeperFastGalleryRepository(StatementOnlySession())
        statement = repository.build_photos_statement(
            filters=FastGalleryFilters(location=location),
            limit=50,
            cursor_datetime=datetime(2024, 1, 1, 8, 0),
            cursor_file_id=123,
        )
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

        relation_predicate = "common_file_metadata.memorykeeper_place_id IS NULL"
        assert relation_predicate in sql
        assert "common_file_metadata.place_name" in sql
        assert sql.index(relation_predicate) < sql.index("LIMIT 51")
        assert sql.index("common_file_metadata.place_name") < sql.index("LIMIT 51")
