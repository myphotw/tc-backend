from __future__ import annotations

from datetime import date, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.model_registry import Base
from app.common.models.change_event import CommonChangeEvent
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.metadata_history import CommonMetadataHistory
from app.common.services.gallery_service import GalleryService
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.schemas.file import MemoryKeeperCaptureDateUpdateRequest
from app.memorykeeper.repositories.fast_gallery_repository import FastGalleryFilters
from app.memorykeeper.services.capture_date_override_service import (
    MemoryKeeperCaptureDateOverrideService,
)
from app.memorykeeper.services.cleanup_group_service import (
    MemoryKeeperCleanupGroupService,
)
from app.memorykeeper.services.fast_gallery_service import MemoryKeeperFastGalleryService


class TestMemoryKeeperCleanupGroups:
    def setup_method(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.groups = MemoryKeeperCleanupGroupService(self.db)
        self.mutations = MemoryKeeperCaptureDateOverrideService(self.db)
        self.counter = 0

    def teardown_method(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _photo(
        self,
        captured_at: datetime | None,
        *,
        basis: str | None,
        raw_capture: datetime | None = None,
        gps: tuple[float, float] | None = None,
        raw_place: str | None = None,
        service_name: str = "MemoryKeeper",
        deleted: bool = False,
    ) -> tuple[CommonFile, CommonFileMetadata, MemoryKeeperFileState]:
        self.counter += 1
        public_id = f"{self.counter:064x}"
        item = CommonFile(
            file_id=public_id,
            original_name=f"{self.counter}.jpg",
            thumb_path=f"thumb/{public_id}.jpg",
            service_name=service_name,
            deleted=deleted,
        )
        self.db.add(item)
        self.db.flush()
        self.db.add(CommonFileService(file_id=item.id, service_name=service_name))
        metadata = CommonFileMetadata(
            file_id=item.id,
            original_capture_datetime=raw_capture,
            gps_lat=gps[0] if gps else None,
            gps_lon=gps[1] if gps else None,
            country="대한민국" if raw_place else None,
            city="서울" if raw_place else None,
            place_name=raw_place,
        )
        state = MemoryKeeperFileState(
            file_id=item.id,
            revision=0,
            effective_capture_datetime=captured_at,
            effective_capture_date=captured_at.date() if captured_at else None,
            effective_capture_year=captured_at.year if captured_at else None,
            date_basis=basis,
        )
        self.db.add_all((metadata, state))
        self.db.commit()
        return item, metadata, state

    def test_place_groups_are_authoritative_stable_and_keyset_paginated(self) -> None:
        first, _, _ = self._photo(
            datetime(2025, 1, 1, 9),
            basis="EXIF",
            raw_place="서울숲",
        )
        second, _, _ = self._photo(
            datetime(2025, 1, 1, 10),
            basis="EXIF",
            raw_place="서울숲",
        )
        third, _, _ = self._photo(
            datetime(2025, 1, 2, 10),
            basis="EXIF",
            raw_place="부산항",
        )

        first_page = self.groups.place_groups(limit=1, cursor=None)
        second_page = self.groups.place_groups(limit=1, cursor=first_page.next_cursor)
        repeated = self.groups.place_groups(limit=10, cursor=None)

        assert first_page.total_groups == 2
        assert first_page.total_photos == 3
        assert first_page.has_more is True
        with pytest.raises(HTTPException) as wrong_queue:
            self.groups.date_groups(limit=1, cursor=first_page.next_cursor)
        assert wrong_queue.value.status_code == 400
        assert second_page.has_more is False
        assert {item.group_id for item in [*first_page.items, *second_page.items]} == {
            item.group_id for item in repeated.items
        }
        seoul_group = next(item for item in repeated.items if item.title == "서울숲")
        assert seoul_group.media_count == 2
        photos = self.groups.place_group_photos(
            group_id=seoul_group.group_id,
            limit=1,
            cursor=None,
        )
        next_photos = self.groups.place_group_photos(
            group_id=seoul_group.group_id,
            limit=1,
            cursor=photos.next_cursor,
        )
        assert photos.total_photos == 2
        assert {photo.file_id for photo in [*photos.items, *next_photos.items]} == {
            first.file_id,
            second.file_id,
        }
        assert third.file_id not in {photo.file_id for photo in photos.items}

    def test_capture_date_groups_use_provenance_and_allow_place_overlap(self) -> None:
        fallback, _, _ = self._photo(
            datetime(2026, 1, 2, 3),
            basis="IMPORTED",
            raw_place=None,
        )
        missing, _, _ = self._photo(None, basis=None, raw_place=None)
        authoritative, _, _ = self._photo(
            datetime(2020, 2, 3, 4),
            basis="EXIF",
            raw_capture=datetime(2020, 2, 3, 4),
        )

        response = self.groups.date_groups(limit=10, cursor=None)
        reasons = {item.cleanup_reason for item in response.items}
        assert reasons == {"MISSING_CAPTURE_DATE", "FALLBACK_DATE_REQUIRES_REVIEW"}
        assert response.total_photos == 2
        fallback_group = next(
            item for item in response.items if item.cleanup_reason == "FALLBACK_DATE_REQUIRES_REVIEW"
        )
        photos = self.groups.date_group_photos(
            group_id=fallback_group.group_id,
            limit=50,
            cursor=None,
        )
        assert [item.file_id for item in photos.items] == [fallback.file_id]
        assert photos.items[0].date_basis == "IMPORTED"
        assert photos.items[0].date_revision == 0
        place_ids = {
            photo.file_id
            for group in self.groups.place_groups(limit=10, cursor=None).items
            for photo in self.groups.place_group_photos(
                group_id=group.group_id,
                limit=50,
                cursor=None,
            ).items
        }
        assert fallback.file_id in place_ids
        assert missing.file_id in place_ids
        assert authoritative.file_id in place_ids

    def test_capture_date_override_and_removal_preserve_raw_metadata_and_place(self) -> None:
        raw = datetime(2010, 5, 6, 7, 8)
        item, metadata, _ = self._photo(
            raw,
            basis="EXIF",
            raw_capture=raw,
            gps=(37.5, 127.0),
            raw_place="서울숲",
        )
        original_place = metadata.place_name
        response = self.mutations.update(
            MemoryKeeperCaptureDateUpdateRequest(
                file_ids=[item.file_id],
                user_capture_date=date(2023, 10, 14),
                expected_date_revisions={item.file_id: 0},
            )
        )
        updated = response.items[0]
        assert updated.user_capture_datetime == datetime(2023, 10, 14)
        assert updated.user_capture_precision == "DATE"
        assert updated.effective_capture_date == date(2023, 10, 14)
        assert updated.date_basis == "USER"
        assert updated.date_revision == 1
        assert metadata.original_capture_datetime == raw
        assert metadata.gps_lat == 37.5
        assert metadata.place_name == original_place

        cleared = self.mutations.update(
            MemoryKeeperCaptureDateUpdateRequest(
                file_ids=[item.file_id],
                user_capture_date=None,
                expected_date_revisions={item.file_id: 1},
            )
        ).items[0]
        assert cleared.user_capture_datetime is None
        assert cleared.user_capture_precision is None
        assert cleared.effective_capture_datetime == raw
        assert cleared.date_basis == "EXIF"
        assert cleared.date_revision == 2
        assert self.db.query(CommonMetadataHistory).filter_by(
            field_name="memorykeeper_user_capture_datetime"
        ).count() == 2
        assert self.db.query(CommonChangeEvent).filter_by(
            resource_type="MemoryKeeperCaptureDate"
        ).count() == 2

    def test_common_gallery_detail_exposes_additive_capture_date_state(self) -> None:
        item, _, _ = self._photo(
            datetime(2020, 2, 3, 4, 5),
            basis="EXIF",
            raw_capture=datetime(2020, 2, 3, 4, 5),
        )
        self.mutations.update(
            MemoryKeeperCaptureDateUpdateRequest(
                file_ids=[item.file_id],
                user_capture_date=date(2023, 10, 14),
                expected_date_revisions={item.file_id: 0},
            )
        )

        detail = GalleryService(self.db).get_detail(
            item.file_id,
            service_name="MemoryKeeper",
        )

        assert detail.user_capture_datetime == datetime(2023, 10, 14)
        assert detail.user_capture_precision == "DATE"
        assert detail.effective_capture_date == date(2023, 10, 14)
        assert detail.effective_capture_year == 2023
        assert detail.date_basis == "USER"
        assert detail.date_revision == detail.metadata_revision == 1

    def test_stale_batch_is_rejected_before_any_override(self) -> None:
        first, _, first_state = self._photo(datetime(2020, 1, 1), basis="EXIF")
        second, _, second_state = self._photo(datetime(2020, 1, 2), basis="EXIF")
        second_state.revision = 2
        self.db.commit()

        with pytest.raises(HTTPException) as conflict:
            self.mutations.update(
                MemoryKeeperCaptureDateUpdateRequest(
                    file_ids=[first.file_id, second.file_id],
                    user_capture_date=date(2024, 4, 5),
                    expected_date_revisions={first.file_id: 0, second.file_id: 0},
                )
            )
        assert conflict.value.status_code == 409
        assert conflict.value.detail["code"] == "REVISION_CONFLICT"
        self.db.expire_all()
        assert self.db.get(MemoryKeeperFileState, first.id).user_capture_datetime is None
        assert self.db.get(MemoryKeeperFileState, second.id).user_capture_datetime is None

    def test_override_moves_fast_gallery_year_and_exposes_date_revision(self) -> None:
        item, _, _ = self._photo(
            datetime(2026, 1, 2, 3),
            basis="IMPORTED",
        )
        place_group_before = self.groups.place_groups(limit=10, cursor=None).items[0].group_id
        assert self.groups.date_groups(limit=10, cursor=None).total_photos == 1
        self.mutations.update(
            MemoryKeeperCaptureDateUpdateRequest(
                file_ids=[item.file_id],
                user_capture_date=date(2023, 10, 14),
                expected_date_revisions={item.file_id: 0},
            )
        )
        gallery = MemoryKeeperFastGalleryService(self.db)
        old_year = gallery.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2026),
        )
        new_year = gallery.photos(
            cursor=None,
            limit=50,
            filters=FastGalleryFilters(year=2023),
        )
        assert old_year.items == []
        assert [photo.file_id for photo in new_year.items] == [item.file_id]
        assert new_year.items[0].date_revision == 1
        assert new_year.items[0].user_capture_precision == "DATE"
        assert self.groups.date_groups(limit=10, cursor=None).total_photos == 0
        place_group_after = self.groups.place_groups(limit=10, cursor=None).items[0].group_id
        assert place_group_after != place_group_before

    def test_group_routes_and_legacy_endpoint_are_registered(self) -> None:
        from app.main import app

        paths = {route.path for route in app.routes if hasattr(route, "path")}
        assert "/api/memorykeeper/place-cleanup" in paths
        assert "/api/memorykeeper/place-cleanup/groups" in paths
        assert "/api/memorykeeper/place-cleanup/groups/{group_id}/photos" in paths
        assert "/api/memorykeeper/capture-date-cleanup/groups" in paths
        assert "/api/memorykeeper/capture-date-cleanup/groups/{group_id}/photos" in paths
        assert "/api/memorykeeper/files/capture-date" in paths

    def test_invalid_cursor_and_unknown_group_are_rejected(self) -> None:
        with pytest.raises(HTTPException) as invalid:
            self.groups.place_groups(limit=5, cursor="not-a-cursor")
        assert invalid.value.status_code == 400

        with pytest.raises(HTTPException) as missing:
            self.groups.place_group_photos(group_id="not-a-group", limit=50, cursor=None)
        assert missing.value.status_code == 404
