from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from PIL.TiffImagePlugin import IFDRational
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.database import Base
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.file_tag import CommonFileTag
from app.common.models.metadata_history import CommonMetadataHistory
from app.common.services.gallery_service import GalleryService
from app.common.services.photo_analysis import ExifReader
from app.common.services.storage_service import StorageService
from scripts.backfill_exif_metadata import backfill_exif_metadata


SAMSUNG_CAPTURE_TEXT = "2026:08:15 14:06:28"
SAMSUNG_CAPTURE_DATETIME = datetime(2026, 8, 15, 14, 6, 28)
SAMSUNG_LATITUDE = 35.2274226997
SAMSUNG_LONGITUDE = 127.5905235997


class LocalStorageService(StorageService):
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def storage_root(self) -> Path:
        return self.root

    @property
    def original_root(self) -> Path:
        return self.root / "original"


def _dms(decimal: float) -> tuple[int, int, float]:
    degrees = int(decimal)
    minutes_float = (decimal - degrees) * 60
    minutes = int(minutes_float)
    seconds = (minutes_float - minutes) * 60
    return degrees, minutes, seconds


def write_exif_image(
    path: Path,
    *,
    exif_ifd: dict[int, object] | None = None,
    top_level: dict[int, object] | None = None,
    gps_ifd: dict[int, object] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    for tag, value in (top_level or {}).items():
        exif[tag] = value
    if exif_ifd:
        exif[34665] = exif_ifd
    if gps_ifd:
        exif[34853] = gps_ifd
    Image.new("RGB", (8, 6), color="navy").save(path, format="JPEG", exif=exif)


class NestedExifIfdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.reader = ExifReader()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_nested_datetime_original_has_priority_and_reads_basic_fields(self) -> None:
        path = self.root / "20260815_140628.jpg"
        write_exif_image(
            path,
            top_level={
                271: "samsung",
                272: "Galaxy S26 Ultra",
                274: 6,
                306: "2020:01:02 03:04:05",
            },
            exif_ifd={
                36867: SAMSUNG_CAPTURE_TEXT,
                36868: "2025:01:01 01:01:01",
                42036: "Samsung Lens",
                34855: 80,
                33434: IFDRational(1, 120),
                33437: IFDRational(9, 5),
                37386: IFDRational(13, 2),
                40962: 4000,
                40963: 3000,
            },
        )

        metadata = self.reader.read(path)

        self.assertEqual(metadata["datetime_original"], SAMSUNG_CAPTURE_DATETIME)
        self.assertEqual(metadata["camera_make"], "samsung")
        self.assertEqual(metadata["camera_model"], "Galaxy S26 Ultra")
        self.assertEqual(metadata["lens"], "Samsung Lens")
        self.assertEqual(metadata["iso"], 80)
        self.assertEqual(metadata["exposure_time"], "1/120")
        self.assertEqual(metadata["f_number"], "9/5")
        self.assertEqual(metadata["focal_length"], "13/2")
        self.assertEqual(metadata["orientation"], 6)
        self.assertEqual(metadata["image_width"], 4000)
        self.assertEqual(metadata["image_height"], 3000)

    def test_nested_datetime_digitized_is_second_fallback(self) -> None:
        path = self.root / "digitized.jpg"
        write_exif_image(
            path,
            exif_ifd={36868: SAMSUNG_CAPTURE_TEXT},
            top_level={306: "2020:01:02 03:04:05"},
        )

        self.assertEqual(
            self.reader.read(path)["datetime_original"],
            SAMSUNG_CAPTURE_DATETIME,
        )

    def test_top_level_datetime_is_third_fallback(self) -> None:
        path = self.root / "top-level.jpg"
        write_exif_image(path, top_level={306: SAMSUNG_CAPTURE_TEXT})

        self.assertEqual(
            self.reader.read(path)["datetime_original"],
            SAMSUNG_CAPTURE_DATETIME,
        )

    def test_missing_or_corrupt_capture_date_does_not_fail(self) -> None:
        missing = self.root / "missing.jpg"
        corrupt = self.root / "corrupt.jpg"
        write_exif_image(missing, top_level={271: "samsung"})
        write_exif_image(corrupt, exif_ifd={36867: "not-a-date"})

        for path in (missing, corrupt):
            with self.subTest(path=path.name):
                metadata = self.reader.read(path)
                self.assertIsNone(metadata.get("datetime_original"))
                self.assertEqual(metadata["image_width"], 8)
                self.assertEqual(metadata["image_height"], 6)

    def test_nested_date_and_gps_match_samsung_reference(self) -> None:
        path = self.root / "20260815_140628.jpg"
        write_exif_image(
            path,
            exif_ifd={36867: SAMSUNG_CAPTURE_TEXT},
            gps_ifd={
                1: "N",
                2: _dms(SAMSUNG_LATITUDE),
                3: "E",
                4: _dms(SAMSUNG_LONGITUDE),
                5: 0,
                6: 42,
            },
        )

        metadata = self.reader.read(path)

        self.assertEqual(metadata["datetime_original"], SAMSUNG_CAPTURE_DATETIME)
        self.assertAlmostEqual(metadata["gps_lat"], SAMSUNG_LATITUDE, places=9)
        self.assertAlmostEqual(metadata["gps_lon"], SAMSUNG_LONGITUDE, places=9)
        self.assertEqual(metadata["gps_alt"], 42.0)


class ExifBackfillTests(unittest.TestCase):
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

    def _add_file_with_locked_metadata(self) -> tuple[CommonFile, CommonFileMetadata, Path]:
        path = self.storage.original_root / "2026" / "Korea" / "20260815_140628.jpg"
        write_exif_image(
            path,
            top_level={271: "samsung", 272: "Galaxy S26 Ultra"},
            exif_ifd={36867: SAMSUNG_CAPTURE_TEXT, 34855: 80},
            gps_ifd={
                1: "N",
                2: _dms(SAMSUNG_LATITUDE),
                3: "E",
                4: _dms(SAMSUNG_LONGITUDE),
            },
        )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        common_file = CommonFile(
            file_id=digest,
            original_name=path.name,
            original_path=self.storage.to_relative_path(path),
            service_name="MemoryKeeper",
            favorite=True,
            deleted=False,
        )
        self.session.add(common_file)
        self.session.flush()
        metadata = CommonFileMetadata(
            file_id=common_file.id,
            datetime_original=None,
            gps_lat=1.25,
            gps_lon=2.5,
            country="사용자 국가",
            province="사용자 도",
            city="사용자 도시",
            district="사용자 구",
            place_name="사용자 장소",
            locked=True,
        )
        self.session.add_all(
            [
                metadata,
                CommonFileService(
                    file_id=common_file.id,
                    service_name="MemoryKeeper",
                ),
                CommonFileTag(
                    file_id=common_file.id,
                    tag="사용자 태그",
                    tag_type="USER",
                    source="USER",
                    deleted=False,
                ),
            ]
        )
        self.session.commit()
        return common_file, metadata, path

    def test_dry_run_reads_original_without_database_or_asset_changes(self) -> None:
        common_file, metadata, path = self._add_file_with_locked_metadata()
        before_bytes = path.read_bytes()
        before_path = common_file.original_path
        before_hash = common_file.file_id

        stats = backfill_exif_metadata(
            self.session,
            storage_service=self.storage,
            execute=False,
        )

        self.assertEqual((stats.scanned, stats.eligible, stats.updated), (1, 1, 1))
        self.session.refresh(metadata)
        self.assertIsNone(metadata.datetime_original)
        self.assertEqual(self.session.query(CommonMetadataHistory).count(), 0)
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(common_file.original_path, before_path)
        self.assertEqual(common_file.file_id, before_hash)

    def test_execute_fills_only_null_exif_fields_and_gallery_uses_capture_date(self) -> None:
        common_file, metadata, path = self._add_file_with_locked_metadata()
        before_bytes = path.read_bytes()
        before_path = common_file.original_path
        before_hash = common_file.file_id

        stats = backfill_exif_metadata(
            self.session,
            storage_service=self.storage,
            execute=True,
        )

        self.assertEqual((stats.updated, stats.failed), (1, 0))
        self.session.refresh(metadata)
        self.assertEqual(metadata.datetime_original, SAMSUNG_CAPTURE_DATETIME)
        self.assertEqual(metadata.camera_make, "samsung")
        self.assertEqual(metadata.camera_model, "Galaxy S26 Ultra")
        self.assertEqual(metadata.iso, 80)
        self.assertEqual(metadata.gps_lat, 1.25)
        self.assertEqual(metadata.gps_lon, 2.5)
        self.assertEqual(metadata.country, "사용자 국가")
        self.assertEqual(metadata.province, "사용자 도")
        self.assertEqual(metadata.city, "사용자 도시")
        self.assertEqual(metadata.district, "사용자 구")
        self.assertEqual(metadata.place_name, "사용자 장소")
        self.assertTrue(common_file.favorite)
        self.assertEqual(self.session.query(CommonFileTag).one().tag, "사용자 태그")
        changed_fields = {
            item.field_name for item in self.session.query(CommonMetadataHistory).all()
        }
        self.assertIn("datetime_original", changed_fields)
        self.assertNotIn("gps_lat", changed_fields)
        self.assertNotIn("gps_lon", changed_fields)
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(common_file.original_path, before_path)
        self.assertEqual(common_file.file_id, before_hash)

        response = GalleryService(self.session).search(service_name="MemoryKeeper")
        self.assertEqual(response.items[0].capture_datetime, SAMSUNG_CAPTURE_DATETIME)

    def test_backfill_rejects_original_path_outside_storage(self) -> None:
        outside = Path(self.temp.name) / "outside.jpg"
        write_exif_image(outside, exif_ifd={36867: SAMSUNG_CAPTURE_TEXT})
        common_file = CommonFile(
            file_id="e" * 64,
            original_name=outside.name,
            original_path=str(outside),
            service_name="MemoryKeeper",
            deleted=False,
        )
        self.session.add(common_file)
        self.session.commit()

        stats = backfill_exif_metadata(
            self.session,
            storage_service=self.storage,
            execute=True,
        )

        self.assertEqual((stats.updated, stats.failed), (0, 1))
        self.assertEqual(self.session.query(CommonFileMetadata).count(), 0)
        self.assertTrue(outside.exists())


if __name__ == "__main__":
    unittest.main()
