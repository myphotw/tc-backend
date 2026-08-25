from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL.TiffImagePlugin import IFDRational
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.database import Base
from app.common.models.api_usage import CommonApiUsage
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.file_tag import CommonFileTag
from app.common.models.metadata_history import CommonMetadataHistory
from app.common.models.vision_job import CommonVisionJob
from app.common.services.gallery_service import GalleryService
from app.common.services.storage_service import StorageService
from app.memorykeeper.services.place_service import MemoryKeeperPlaceService
from scripts.backfill_photo_metadata import backfill_photo_metadata
from tests.test_exif_nested_ifd import (
    SAMSUNG_CAPTURE_DATETIME,
    SAMSUNG_CAPTURE_TEXT,
    SAMSUNG_LATITUDE,
    SAMSUNG_LONGITUDE,
    LocalStorageService,
    _dms,
    write_exif_image,
)
from worker.plugins.base import PluginContext
from worker.plugins.exif_plugin import ExifPlugin
from worker.plugins.gps_plugin import GpsPlugin
from worker.plugins.metadata_plugin import MetadataPlugin


GEOGRAPHY_RESULT = {
    "provider": "google_geocoding",
    "status": "ok",
    "latitude": SAMSUNG_LATITUDE,
    "longitude": SAMSUNG_LONGITUDE,
    "country": "대한민국",
    "province": "경상남도",
    "city": "창원시",
    "district": "마산합포구",
    "place_name": "대한민국 경상남도 창원시 마산합포구",
}


def write_samsung_fixture(path: Path) -> None:
    write_exif_image(
        path,
        top_level={
            271: "samsung",
            272: "Galaxy S26 Ultra",
            306: SAMSUNG_CAPTURE_TEXT,
        },
        exif_ifd={
            36867: SAMSUNG_CAPTURE_TEXT,
            36868: SAMSUNG_CAPTURE_TEXT,
            42036: "Samsung Lens",
            34855: 80,
            33434: IFDRational(1, 120),
            33437: IFDRational(9, 5),
            37386: IFDRational(13, 2),
        },
        gps_ifd={
            1: "N",
            2: _dms(SAMSUNG_LATITUDE),
            3: "E",
            4: _dms(SAMSUNG_LONGITUDE),
        },
    )


class FakeGeocodingClient:
    def __init__(self, **_: object) -> None:
        pass

    def reverse_geocode(self, **_: float) -> dict[str, object]:
        return dict(GEOGRAPHY_RESULT)


class UploadMetadataPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = LocalStorageService(Path(self.temp.name) / "PhotoPlatform")
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()
        self.temp.cleanup()

    def test_upload_plugins_persist_exif_gps_geography_and_gallery_projection(self) -> None:
        path = self.storage.original_root / "2026" / "20260815_140628.jpg"
        write_samsung_fixture(path)
        common_file = CommonFile(
            file_id=hashlib.sha256(path.read_bytes()).hexdigest(),
            original_name=path.name,
            original_path=self.storage.to_relative_path(path),
            service_name="MemoryKeeper",
            deleted=False,
        )
        self.session.add(common_file)
        self.session.flush()
        self.session.add(
            CommonFileService(file_id=common_file.id, service_name="MemoryKeeper")
        )
        self.session.commit()
        context = PluginContext(
            db=self.session,
            storage_service=self.storage,
            common_file=common_file,
            original_path=path,
            service_name="MemoryKeeper",
        )

        MetadataPlugin().run(context)
        ExifPlugin().run(context)
        with (
            patch(
                "worker.plugins.gps_plugin.GeocodingClient",
                FakeGeocodingClient,
            ),
            patch.object(
                MemoryKeeperPlaceService,
                "auto_match_file",
                return_value=False,
            ),
        ):
            GpsPlugin().run(context)

        metadata = self.session.query(CommonFileMetadata).one()
        self.assertEqual(metadata.datetime_original, SAMSUNG_CAPTURE_DATETIME)
        self.assertEqual(metadata.camera_make, "samsung")
        self.assertEqual(metadata.camera_model, "Galaxy S26 Ultra")
        self.assertEqual(metadata.lens, "Samsung Lens")
        self.assertEqual(metadata.exposure_time, "1/120")
        self.assertEqual(metadata.f_number, "9/5")
        self.assertEqual(metadata.iso, 80)
        self.assertEqual(metadata.focal_length, "13/2")
        self.assertAlmostEqual(metadata.gps_lat, SAMSUNG_LATITUDE, places=9)
        self.assertAlmostEqual(metadata.gps_lon, SAMSUNG_LONGITUDE, places=9)
        self.assertEqual(metadata.country, "대한민국")
        self.assertEqual(metadata.province, "경상남도")
        self.assertEqual(metadata.city, "창원시")
        self.assertEqual(metadata.district, "마산합포구")
        self.assertEqual(metadata.place_name, GEOGRAPHY_RESULT["place_name"])

        detail = GalleryService(self.session).get_detail(
            common_file.file_id,
            service_name="MemoryKeeper",
        )
        self.assertEqual(
            detail.metadata["datetime_original"],
            SAMSUNG_CAPTURE_DATETIME,
        )
        self.assertEqual(detail.metadata["camera_make"], "samsung")
        self.assertEqual(detail.metadata["camera_model"], "Galaxy S26 Ultra")
        self.assertEqual(detail.metadata["place_name"], GEOGRAPHY_RESULT["place_name"])


class PhotoMetadataBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = LocalStorageService(Path(self.temp.name) / "PhotoPlatform")
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()
        self.temp.cleanup()

    def _add_file(self, **metadata_values: object) -> tuple[CommonFile, CommonFileMetadata, Path]:
        path = self.storage.original_root / "2026" / "20260815_140628.jpg"
        write_samsung_fixture(path)
        common_file = CommonFile(
            file_id=hashlib.sha256(path.read_bytes()).hexdigest(),
            original_name=path.name,
            original_path=self.storage.to_relative_path(path),
            service_name="MemoryKeeper",
            deleted=False,
        )
        self.session.add(common_file)
        self.session.flush()
        metadata = CommonFileMetadata(file_id=common_file.id, **metadata_values)
        self.session.add_all(
            [
                metadata,
                CommonFileService(
                    file_id=common_file.id,
                    service_name="MemoryKeeper",
                ),
            ]
        )
        self.session.commit()
        return common_file, metadata, path

    def test_dry_run_reports_reference_file_without_writes_or_provider_call(self) -> None:
        _, metadata, _ = self._add_file(
            gps_lat=SAMSUNG_LATITUDE,
            gps_lon=SAMSUNG_LONGITUDE,
        )
        calls: list[tuple[float, float]] = []

        stats = backfill_photo_metadata(
            self.session,
            storage_service=self.storage,
            filename="20260815_140628.jpg",
            execute=False,
            geocoder=lambda lat, lon: calls.append((lat, lon)) or GEOGRAPHY_RESULT,
        )

        self.assertEqual(stats.inspected_files, 1)
        self.assertEqual(stats.exif_backfill_targets, 1)
        self.assertEqual(stats.geography_backfill_targets, 1)
        self.assertEqual(stats.would_update_files, 1)
        self.assertEqual(calls, [])
        self.session.refresh(metadata)
        self.assertIsNone(metadata.datetime_original)
        self.assertIsNone(metadata.country)
        self.assertEqual(self.session.query(CommonMetadataHistory).count(), 0)

    def test_apply_fills_exif_and_geography_nulls_without_overwrite(self) -> None:
        _, metadata, path = self._add_file(
            camera_make="Existing Make",
            gps_lat=SAMSUNG_LATITUDE,
            gps_lon=SAMSUNG_LONGITUDE,
            country="기존 국가",
            city="",
        )
        before_bytes = path.read_bytes()

        stats = backfill_photo_metadata(
            self.session,
            storage_service=self.storage,
            execute=True,
            geocoder=lambda _lat, _lon: dict(GEOGRAPHY_RESULT),
        )

        self.assertEqual(stats.updated_files, 1)
        self.session.refresh(metadata)
        self.assertEqual(metadata.camera_make, "Existing Make")
        self.assertEqual(metadata.camera_model, "Galaxy S26 Ultra")
        self.assertEqual(metadata.datetime_original, SAMSUNG_CAPTURE_DATETIME)
        self.assertEqual(metadata.country, "기존 국가")
        self.assertEqual(metadata.province, "경상남도")
        self.assertEqual(metadata.city, "창원시")
        self.assertEqual(metadata.district, "마산합포구")
        self.assertEqual(metadata.place_name, GEOGRAPHY_RESULT["place_name"])
        self.assertAlmostEqual(metadata.gps_lat, SAMSUNG_LATITUDE, places=9)
        self.assertAlmostEqual(metadata.gps_lon, SAMSUNG_LONGITUDE, places=9)
        self.assertEqual(path.read_bytes(), before_bytes)

    def test_missing_gps_skips_geography(self) -> None:
        self._add_file()

        stats = backfill_photo_metadata(
            self.session,
            storage_service=self.storage,
            execute=False,
        )

        self.assertEqual(stats.exif_backfill_targets, 1)
        self.assertEqual(stats.geography_backfill_targets, 0)

    def test_backfill_does_not_change_vision_usage_jobs_or_tags(self) -> None:
        common_file, _, _ = self._add_file(
            gps_lat=SAMSUNG_LATITUDE,
            gps_lon=SAMSUNG_LONGITUDE,
        )
        usage = CommonApiUsage(
            provider="GOOGLE",
            api_name="VISION",
            year=2026,
            month=8,
            used_unit=7,
            limit_unit=900,
            remaining_unit=893,
            deleted=False,
        )
        vision_job = CommonVisionJob(
            file_id=common_file.id,
            status="WAITING",
            vision_provider="GOOGLE",
            deleted=False,
        )
        tag = CommonFileTag(
            file_id=common_file.id,
            tag="Sky",
            tag_type="AI",
            source="AI",
            confidence=0.9,
            deleted=False,
        )
        self.session.add_all([usage, vision_job, tag])
        self.session.commit()
        before = (usage.used_unit, vision_job.status, tag.tag, tag.confidence)

        backfill_photo_metadata(
            self.session,
            storage_service=self.storage,
            execute=True,
            geocoder=lambda _lat, _lon: dict(GEOGRAPHY_RESULT),
        )

        self.session.refresh(usage)
        self.session.refresh(vision_job)
        self.session.refresh(tag)
        self.assertEqual(
            (usage.used_unit, vision_job.status, tag.tag, tag.confidence),
            before,
        )
        self.assertEqual(self.session.query(CommonVisionJob).count(), 1)
        self.assertEqual(self.session.query(CommonFileTag).count(), 1)

    def test_apply_is_idempotent(self) -> None:
        _, metadata, _ = self._add_file(
            gps_lat=SAMSUNG_LATITUDE,
            gps_lon=SAMSUNG_LONGITUDE,
        )
        geocoder_calls: list[tuple[float, float]] = []

        def geocode(latitude: float, longitude: float) -> dict[str, object]:
            geocoder_calls.append((latitude, longitude))
            return dict(GEOGRAPHY_RESULT)

        first = backfill_photo_metadata(
            self.session,
            storage_service=self.storage,
            execute=True,
            geocoder=geocode,
        )
        history_count = self.session.query(CommonMetadataHistory).count()
        self.session.refresh(metadata)
        values_after_first = {
            column: getattr(metadata, column)
            for column in (
                "camera_make",
                "camera_model",
                "datetime_original",
                "country",
                "province",
                "city",
                "district",
                "place_name",
            )
        }

        second = backfill_photo_metadata(
            self.session,
            storage_service=self.storage,
            execute=True,
            geocoder=geocode,
        )

        self.session.refresh(metadata)
        self.assertEqual(first.updated_files, 1)
        self.assertEqual(second.updated_files, 0)
        self.assertEqual(second.already_complete_skipped, 1)
        self.assertEqual(len(geocoder_calls), 1)
        self.assertEqual(self.session.query(CommonMetadataHistory).count(), history_count)
        self.assertEqual(
            {column: getattr(metadata, column) for column in values_after_first},
            values_after_first,
        )


if __name__ == "__main__":
    unittest.main()
