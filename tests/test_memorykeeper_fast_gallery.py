from __future__ import annotations

from datetime import date, datetime, timedelta
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
        city: str | None = "서울",
    ) -> MemoryKeeperPlace:
        place = MemoryKeeperPlace(
            id=str(uuid4()),
            display_name=display_name,
            canonical_name=display_name,
            country=country,
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
        city: str | None = "서울",
        place_name: str | None = "원시 장소",
        date_basis: str | None = "EXIF",
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
                effective_capture_year=(captured_at.year if captured_at else None),
                date_basis=date_basis if captured_at else None,
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
        assert len(statements) == 5  # four summary aggregates + one hierarchy GROUP BY
        assert len(hierarchy.items) == 2
        assert hierarchy.items[0].year == 2025
        assert hierarchy.items[0].count == 2
        assert hierarchy.items[0].countries[0].regions[0].places[0].display_name == "서울숲"
        assert hierarchy.items[0].countries[0].regions[0].places[0].location_key == (
            encode_registered_location_key(place.id)
        )
        assert hierarchy.items[1].countries[0].country is None

    def test_hierarchy_assigns_distinct_registered_and_raw_location_keys(self) -> None:
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
            leaf
            for leaf in leaves
            if leaf.memorykeeper_place_id is None
            and leaf.display_name == "40 Bise 海辺"
        )

        assert registered_leaf.location_key == encode_registered_location_key(
            registered.id
        )
        raw_identity = decode_location_key(raw_leaf.location_key)
        assert (raw_identity.country, raw_identity.region, raw_identity.place) == (
            "일본",
            "Motobu",
            "40 Bise 海辺",
        )

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

    def test_deleted_registered_place_becomes_one_raw_hierarchy_leaf(self) -> None:
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
        assert leaves[0].memorykeeper_place_id is None
        identity = decode_location_key(leaves[0].location_key)
        assert identity.kind == "raw"
        assert (identity.country, identity.region, identity.place) == (
            "일본",
            "Motobu",
            "raw fallback",
        )
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
        # Legacy Recent/Pending operate on active MemoryKeeper membership, not
        # the capture-date projection used by the canonical fast photo list.
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
        assert summary.pending_count == 2
        assert summary.place_cleanup_count == 3
        assert payload["favorite_count"] == 0
        assert payload["recent_count"] == 3
        assert payload["pending_count"] == 2
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
        assert "location_key" in {parameter["name"] for parameter in parameters}

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
        assert "ORDER BY gallery_candidates.effective_capture_datetime DESC" in sql

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

        assert "memorykeeper_places.id IS NULL" in sql
        assert "common_file_metadata.place_name" in sql
        assert sql.index("memorykeeper_places.id IS NULL") < sql.index("LIMIT 51")
        assert sql.index("common_file_metadata.place_name") < sql.index("LIMIT 51")
