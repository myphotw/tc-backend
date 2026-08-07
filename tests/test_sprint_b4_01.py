from __future__ import annotations

from datetime import datetime, timezone
import unittest

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.astrojournal.models.observation_record import ObservationRecord
from app.astrojournal.services.gallery_service import AstroGalleryService
from app.common.database import Base
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.schemas.gallery import GalleryListItem
from app.main import app


class SprintB401Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.service = AstroGalleryService(self.session)

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _file(
        self,
        digest: str,
        *,
        service_name: str = "AstroJournal",
        with_media: bool = True,
        deleted: bool = False,
    ) -> CommonFile:
        common_file = CommonFile(
            file_id=digest,
            original_name=f"{digest[:8]}.fits",
            extension=".fits",
            mime_type="image/fits",
            original_path=f"original/{digest}.fits" if with_media else None,
            preview_path=f"preview/{digest}.jpg" if with_media else None,
            thumb_path=f"thumb/{digest}.jpg" if with_media else None,
            deleted=deleted,
        )
        self.session.add(common_file)
        self.session.flush()
        self.session.add(
            CommonFileService(file_id=common_file.id, service_name=service_name)
        )
        return common_file

    def _record(
        self,
        common_file: CommonFile,
        *,
        catalog_object_id: str = "M42",
        captured_at: datetime,
        favorite: bool = False,
        representative: bool = False,
        deleted_at: datetime | None = None,
    ) -> ObservationRecord:
        record = ObservationRecord(
            file_id=common_file.id,
            catalog_object_id=catalog_object_id,
            captured_at=captured_at,
            latitude=37.5,
            longitude=127.0,
            location_name="Observatory",
            memo="Clear night",
            favorite=favorite,
            representative=representative,
            deleted_at=deleted_at,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def test_projection_joins_record_file_metadata_and_urls(self) -> None:
        common_file = self._file("1" * 64)
        capture_datetime = datetime(2026, 1, 1, 21, 30, tzinfo=timezone.utc)
        self.session.add(
            CommonFileMetadata(
                file_id=common_file.id,
                datetime_original=capture_datetime,
            )
        )
        record = self._record(
            common_file,
            captured_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            favorite=True,
            representative=True,
        )
        self.session.commit()

        item = self.service.get_detail(record.id)

        self.assertEqual(str(item.record_id), record.id)
        self.assertEqual(item.revision, 1)
        self.assertEqual(item.catalog_object_id, "M42")
        self.assertTrue(item.favorite)
        self.assertTrue(item.representative)
        self.assertEqual(item.file_id, common_file.file_id)
        self.assertEqual(item.filename, common_file.original_name)
        self.assertEqual(item.mime_type, "image/fits")
        self.assertEqual(
            item.thumbnail_url,
            f"/api/common/gallery/{common_file.file_id}/thumbnail",
        )
        self.assertEqual(
            item.preview_url,
            f"/api/common/gallery/{common_file.file_id}/preview",
        )
        self.assertEqual(
            item.original_url,
            f"/api/common/gallery/{common_file.file_id}/original",
        )
        self.assertEqual(item.capture_datetime.replace(tzinfo=timezone.utc), capture_datetime)

    def test_missing_media_paths_return_null_urls(self) -> None:
        common_file = self._file("2" * 64, with_media=False)
        record = self._record(
            common_file,
            captured_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )
        self.session.commit()

        item = self.service.get_detail(record.id)
        self.assertIsNone(item.thumbnail_url)
        self.assertIsNone(item.preview_url)
        self.assertIsNone(item.original_url)

    def test_excludes_soft_deleted_missing_link_and_deleted_file(self) -> None:
        visible_file = self._file("3" * 64)
        visible = self._record(
            visible_file,
            captured_at=datetime(2026, 2, 4, tzinfo=timezone.utc),
        )
        memory_file = self._file("4" * 64, service_name="MemoryKeeper")
        hidden_by_link = self._record(
            memory_file,
            captured_at=datetime(2026, 2, 3, tzinfo=timezone.utc),
        )
        deleted_file = self._file("5" * 64, deleted=True)
        hidden_by_file = self._record(
            deleted_file,
            captured_at=datetime(2026, 2, 2, tzinfo=timezone.utc),
        )
        soft_deleted_file = self._file("6" * 64)
        hidden_by_record = self._record(
            soft_deleted_file,
            captured_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            deleted_at=datetime(2026, 2, 5, tzinfo=timezone.utc),
        )
        self.session.commit()

        response = self.service.list_gallery()
        self.assertEqual(response.total, 1)
        self.assertEqual(str(response.items[0].record_id), visible.id)
        for record in (hidden_by_link, hidden_by_file, hidden_by_record):
            with self.assertRaises(HTTPException) as raised:
                self.service.get_detail(record.id)
            self.assertEqual(raised.exception.status_code, 404)

    def test_filters_sort_and_pagination(self) -> None:
        oldest_file = self._file("7" * 64)
        newest_file = self._file("8" * 64)
        other_file = self._file("9" * 64)
        oldest = self._record(
            oldest_file,
            captured_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            favorite=True,
        )
        newest = self._record(
            newest_file,
            captured_at=datetime(2026, 3, 3, tzinfo=timezone.utc),
            favorite=True,
        )
        self._record(
            other_file,
            catalog_object_id="M31",
            captured_at=datetime(2026, 3, 2, tzinfo=timezone.utc),
            favorite=False,
        )
        self.session.commit()

        response = self.service.list_gallery(
            catalog_object_id="M42",
            favorite=True,
            date_from=datetime(2026, 3, 1, tzinfo=timezone.utc),
            date_to=datetime(2026, 3, 3, tzinfo=timezone.utc),
            page=1,
            page_size=1,
        )

        self.assertEqual(response.total, 2)
        self.assertEqual(str(response.items[0].record_id), newest.id)
        page_two = self.service.list_gallery(
            catalog_object_id="M42",
            favorite=True,
            page=2,
            page_size=1,
        )
        self.assertEqual(str(page_two.items[0].record_id), oldest.id)

    def test_not_found_and_openapi_contract(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            self.service.get_detail("00000000-0000-0000-0000-000000000000")
        self.assertEqual(raised.exception.status_code, 404)

        paths = app.openapi()["paths"]
        self.assertEqual(set(paths["/api/astro/gallery"]), {"get"})
        self.assertEqual(set(paths["/api/astro/gallery/{record_id}"]), {"get"})
        self.assertFalse(
            {"record_id", "revision", "catalog_object_id"}
            & set(GalleryListItem.model_fields)
        )
