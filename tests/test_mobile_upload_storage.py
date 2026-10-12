from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, UploadFile
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.database import Base
from app.common.models.file import CommonFile
from app.common.models.upload_job import UploadJob, UploadSourceType
from app.common.repositories.upload_job_repository import UploadJobRepository
from app.common.routers import upload as upload_router
from app.common.services.gallery_service import GalleryService
from app.common.services.media_derivatives import MediaDerivativeService
from app.common.services.media_probe import MediaProbe
from app.common.services.storage_service import AssetDeleteStatus, StorageService
from worker import background_worker


class LocalStorageService(StorageService):
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def storage_root(self) -> Path:
        return self.root

    @property
    def incoming_root(self) -> Path:
        return self.root / "incoming"

    @property
    def original_root(self) -> Path:
        return self.root / "original"

    @property
    def mobile_original_root(self) -> Path:
        return self.root / "mobile-original"

    @property
    def preview_root(self) -> Path:
        return self.root / "preview"

    @property
    def thumb_root(self) -> Path:
        return self.root / "thumb"


class MobileUploadStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = LocalStorageService(Path(self.temp.name) / "PhotoPlatform")
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session = self.session_factory()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()
        self.temp.cleanup()

    @staticmethod
    def _jpeg_bytes(color: str) -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (32, 24), color=color).save(output, format="JPEG")
        return output.getvalue()

    def _upload(self, content: bytes, *, upload_source: str | None = None) -> UploadJob:
        with patch.object(upload_router, "storage_service", self.storage):
            response = upload_router.upload_file(
                file=UploadFile(filename="photo.jpg", file=io.BytesIO(content)),
                service_name="MemoryKeeper",
                upload_source=upload_source,
                client_file_id=None,
                client_content_sha256=None,
                db=self.session,
            )
        return self.session.query(UploadJob).filter_by(job_id=response["job_id"]).one()

    def _process(self, job: UploadJob) -> CommonFile:
        with patch.object(
            background_worker,
            "StorageService",
            return_value=self.storage,
        ), patch.object(
            background_worker,
            "SessionLocal",
            self.session_factory,
        ):
            self.assertTrue(
                background_worker.process_next_job(
                    worker_id="mobile-storage-test",
                    db=self.session,
                )
            )
        self.session.refresh(job)
        self.assertEqual(job.status, "COMPLETED")
        return self.session.query(CommonFile).filter_by(file_id=job.file_id).one()

    def test_default_upload_keeps_legacy_job_source_and_original_root(self) -> None:
        job = self._upload(self._jpeg_bytes("navy"))

        self.assertEqual(job.source_type, UploadSourceType.UPLOAD)
        common_file = self._process(job)

        self.assertTrue(common_file.original_path.startswith("original/"))
        self.assertFalse(common_file.original_path.startswith("mobile-original/"))

    def test_mobile_job_survives_claim_and_selects_mobile_original_root(self) -> None:
        job = self._upload(self._jpeg_bytes("green"), upload_source="mobile")

        self.assertEqual(job.source_type, UploadSourceType.UPLOAD_MOBILE)
        common_file = self._process(job)

        self.assertTrue(common_file.original_path.startswith("mobile-original/"))
        self.assertTrue(
            self.storage.resolve_original_path(common_file.original_path).is_file()
        )

    def test_mobile_duplicate_reuses_existing_legacy_original(self) -> None:
        content = self._jpeg_bytes("purple")
        original_job = self._upload(content)
        original_file = self._process(original_job)
        original_path = original_file.original_path

        mobile_job = self._upload(content, upload_source="mobile")
        duplicate_file = self._process(mobile_job)

        self.assertEqual(self.session.query(CommonFile).count(), 1)
        self.assertEqual(duplicate_file.id, original_file.id)
        self.assertEqual(duplicate_file.original_path, original_path)
        self.assertFalse(self.storage.mobile_original_root.exists())

    def test_mobile_original_is_served_regenerated_and_safely_deleted(self) -> None:
        job = self._upload(self._jpeg_bytes("orange"), upload_source="mobile")
        common_file = self._process(job)

        served_path, media_type = GalleryService(
            self.session,
            storage_service=self.storage,
        ).get_media(
            file_id=common_file.file_id,
            kind="original",
        )
        self.assertEqual(
            served_path,
            self.storage.resolve_original_path(common_file.original_path),
        )
        self.assertEqual(media_type, "image/jpeg")

        result = MediaDerivativeService(self.storage).generate(
            original_path=common_file.original_path,
            file_id=common_file.file_id,
            media=MediaProbe().probe_for_service(
                served_path,
                filename=common_file.original_name,
                service_name="MemoryKeeper",
            ),
        )
        self.assertIsNotNone(result.preview_path)
        self.assertIsNotNone(result.thumb_path)

        paths = {
            "original": common_file.original_path,
            "preview": common_file.preview_path,
            "thumb": common_file.thumb_path,
        }
        cleanup = self.storage.delete_common_file_assets(common_file)
        self.assertEqual(cleanup["original"], AssetDeleteStatus.DELETED)
        self.assertEqual(cleanup["preview"], AssetDeleteStatus.DELETED)
        self.assertEqual(cleanup["thumb"], AssetDeleteStatus.DELETED)
        for stored_path in paths.values():
            self.assertFalse(self.storage.resolve_storage_path(stored_path).exists())

    def test_mobile_original_marker_is_not_collapsed_to_legacy_original(self) -> None:
        relative_path = "mobile-original/2026/Unknown/photo.jpg"
        expected = (self.storage.storage_root / relative_path).resolve(strict=False)

        self.assertEqual(
            self.storage.resolve_storage_path(relative_path),
            expected,
        )
        self.assertEqual(
            self.storage.resolve_storage_path(
                f"/volume1/photo/PhotoPlatform/{relative_path}"
            ),
            expected,
        )
        self.assertNotEqual(
            self.storage.resolve_storage_path(relative_path),
            (self.storage.original_root / "2026/Unknown/photo.jpg").resolve(
                strict=False
            ),
        )

    def test_invalid_or_non_memorykeeper_mobile_source_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as invalid:
            upload_router._validate_upload_source(
                "desktop",
                service_name="MemoryKeeper",
            )
        self.assertEqual(invalid.exception.status_code, 422)

        with self.assertRaises(HTTPException) as astro:
            upload_router._validate_upload_source(
                "mobile",
                service_name="AstroJournal",
            )
        self.assertEqual(astro.exception.status_code, 422)

    def test_idempotent_replay_rejects_a_different_upload_source(self) -> None:
        repository = UploadJobRepository(self.session)
        job = repository.create_waiting_job(
            job_id="11111111-1111-1111-1111-111111111111",
            source_type=UploadSourceType.UPLOAD,
            incoming_path="incoming/photo.jpg",
            service_name="MemoryKeeper",
            client_file_id="stable-mobile-id",
        )

        with self.assertRaises(HTTPException) as conflict:
            upload_router._idempotent_response(
                repository,
                job,
                None,
                requested_source_type=UploadSourceType.UPLOAD_MOBILE,
            )
        self.assertEqual(conflict.exception.status_code, 409)

    def test_relative_traversal_and_unconfigured_original_root_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "escapes PHOTO_PLATFORM_ROOT"):
            self.storage.resolve_storage_path("../outside/photo.jpg")

        outside = Path(self.temp.name) / "outside"
        incoming = self.storage.incoming_root / "photo.jpg"
        incoming.parent.mkdir(parents=True, exist_ok=True)
        incoming.write_bytes(b"photo")
        with self.assertRaisesRegex(ValueError, "not an allowed original root"):
            self.storage.move_to_storage(
                incoming,
                "a" * 64,
                ".jpg",
                destination_root=outside,
            )


if __name__ == "__main__":
    unittest.main()
