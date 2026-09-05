from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.common.model_registry import Base
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.place import MemoryKeeperPlace
from app.memorykeeper.services.fast_gallery_service import MemoryKeeperFastGalleryService
from app.memorykeeper.services.place_cleanup_service import (
    MemoryKeeperPlaceCleanupService,
)
from app.memorykeeper.services.place_service import MemoryKeeperPlaceService


class TestMemoryKeeperPlaceCleanup:
    def setup_method(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.fast_gallery = MemoryKeeperFastGalleryService(self.db)
        self.place_cleanup = MemoryKeeperPlaceCleanupService(self.db)
        self.counter = 0

    def teardown_method(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _place(
        self,
        *,
        country: str | None,
        city: str | None,
        province: str | None = None,
    ) -> MemoryKeeperPlace:
        place = MemoryKeeperPlace(
            id=str(uuid4()),
            display_name=f"장소-{uuid4()}",
            canonical_name=None,
            country=country,
            city=city,
            province=province,
            latitude=37.5,
            longitude=127.0,
            active=True,
        )
        self.db.add(place)
        self.db.flush()
        return place

    def _photo(
        self,
        *,
        captured_at: datetime,
        place: MemoryKeeperPlace | None = None,
        raw_country: str | None = None,
        raw_city: str | None = None,
        raw_province: str | None = None,
        raw_place_name: str | None = None,
        metadata: bool = True,
        favorite: bool = False,
        deleted: bool = False,
        service_name: str = "MemoryKeeper",
    ) -> CommonFile:
        self.counter += 1
        public_id = f"{self.counter:064x}"
        common_file = CommonFile(
            file_id=public_id,
            original_name=f"{self.counter}.jpg",
            thumb_path=f"thumb/{public_id}.jpg",
            deleted=deleted,
            service_name=service_name,
        )
        self.db.add(common_file)
        self.db.flush()
        self.db.add(
            CommonFileService(
                file_id=common_file.id,
                service_name=service_name,
            )
        )
        if metadata:
            self.db.add(
                CommonFileMetadata(
                    file_id=common_file.id,
                    datetime_original=captured_at,
                    country=raw_country,
                    city=raw_city,
                    province=raw_province,
                    place_name=raw_place_name,
                    memorykeeper_place_id=place.id if place else None,
                )
            )
        self.db.add(
            MemoryKeeperFileState(
                file_id=common_file.id,
                favorite=favorite,
                effective_capture_datetime=captured_at,
                effective_capture_date=captured_at.date(),
                effective_capture_year=captured_at.year,
                date_basis="EXIF",
            )
        )
        self.db.commit()
        return common_file

    def test_summary_and_list_use_the_same_unique_cleanup_set(self) -> None:
        captured = datetime(2025, 1, 1, 8, 0)
        complete = self._place(country="대한민국", city="서울")
        missing_country = self._place(country=None, city="서울")
        missing_region = self._place(country="대한민국", city=None)
        blank_country = self._place(country=" ", city="서울")

        normal = self._photo(
            captured_at=captured,
            place=complete,
            favorite=True,
        )
        pending_only = self._photo(
            captured_at=captured + timedelta(days=1),
            raw_country="대한민국",
            raw_city="서울",
            raw_place_name="원시 장소",
        )
        unclassified_country = self._photo(
            captured_at=captured + timedelta(days=2),
            place=missing_country,
        )
        unclassified_region = self._photo(
            captured_at=captured + timedelta(days=3),
            place=missing_region,
        )
        blank_hierarchy = self._photo(
            captured_at=captured + timedelta(days=4),
            place=blank_country,
        )
        both = self._photo(captured_at=captured + timedelta(days=5))
        without_metadata = self._photo(
            captured_at=captured + timedelta(days=6),
            metadata=False,
        )
        self._photo(
            captured_at=captured + timedelta(days=7),
            deleted=True,
        )
        self._photo(
            captured_at=captured + timedelta(days=8),
            service_name="AstroJournal",
        )

        summary = self.fast_gallery.summary()
        pages = [
            self.place_cleanup.list(page=page, page_size=2)
            for page in range(1, 4)
        ]
        listed_ids = [item.file_id for page in pages for item in page.items]

        assert summary.total_photos == 7
        assert summary.favorite_count == 1
        assert summary.recent_count == 7
        assert summary.pending_count == 3
        assert summary.place_cleanup_count == 6
        assert all(page.total == 6 for page in pages)
        assert len(listed_ids) == len(set(listed_ids)) == 6
        assert normal.file_id not in listed_ids
        assert {
            pending_only.file_id,
            unclassified_country.file_id,
            unclassified_region.file_id,
            blank_hierarchy.file_id,
            both.file_id,
            without_metadata.file_id,
        } == set(listed_ids)

        by_id = {
            item.file_id: item
            for page in pages
            for item in page.items
        }
        assert by_id[pending_only.file_id].memorykeeper_place_id is None
        assert (
            str(by_id[unclassified_country.file_id].memorykeeper_place_id)
            == missing_country.id
        )

    def test_successful_place_mapping_removes_both_cleanup_reasons(self) -> None:
        captured = datetime(2025, 2, 1, 8, 0)
        complete = self._place(country="대한민국", city="서울")
        missing_country = self._place(country=None, city="서울")
        pending_only = self._photo(
            captured_at=captured,
            raw_country="대한민국",
            raw_city="서울",
            raw_place_name="원시 장소",
        )
        unclassified_only = self._photo(
            captured_at=captured + timedelta(days=1),
            place=missing_country,
        )

        before = self.fast_gallery.summary()
        assert before.pending_count == 1
        assert before.place_cleanup_count == 2

        places = MemoryKeeperPlaceService(self.db)
        places.assign_file(
            public_file_id=pending_only.file_id,
            place_id=complete.id,
            expected_revision=0,
        )
        after_pending = self.fast_gallery.summary()
        assert after_pending.pending_count == 0
        assert after_pending.place_cleanup_count == 1

        places.assign_file(
            public_file_id=unclassified_only.file_id,
            place_id=complete.id,
            expected_revision=0,
        )
        after_all = self.fast_gallery.summary()
        listing = self.place_cleanup.list(page=1, page_size=50)

        assert after_all.pending_count == 0
        assert after_all.place_cleanup_count == 0
        assert listing.total == 0
        assert listing.items == []

    def test_list_uses_only_count_and_page_queries(self) -> None:
        captured = datetime(2025, 3, 1, 8, 0)
        for day in range(3):
            self._photo(captured_at=captured + timedelta(days=day))
        statements: list[str] = []

        def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            response = self.place_cleanup.list(page=1, page_size=2)
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)

        assert response.total == 3
        assert len(response.items) == 2
        assert len(statements) == 2
