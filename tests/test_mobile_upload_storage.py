from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException, UploadFile
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.database import Base
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.metadata_history import CommonMetadataHistory
from app.common.models.upload_job import UploadJob, UploadSourceType
from app.common.repositories.upload_job_repository import UploadJobRepository
from app.common.routers import upload as upload_router
from app.common.services.gallery_service import GalleryService
from app.common.services.media_derivatives import MediaDerivativeService
from app.common.services.media_probe import MediaProbe
from app.common.services.storage_service import AssetDeleteStatus, StorageService
from app.common.services.upload_job_service import UploadJobService
from app.common.services.upload_validation import (
    UploadFailureCode,
    UploadValidationError,
)
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from worker import background_worker
from worker.plugins.base import PluginContext
from worker.plugins.plugin_manager import PluginManager


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

    def _upload(
        self,
        content: bytes,
        *,
        upload_source: str | None = None,
        service_name: str = "MemoryKeeper",
        client_file_id: str | None = None,
        client_content_sha256: str | None = None,
    ) -> UploadJob:
        with patch.object(upload_router, "storage_service", self.storage):
            response = upload_router.upload_file(
                file=UploadFile(filename="photo.jpg", file=io.BytesIO(content)),
                service_name=service_name,
                upload_source=upload_source,
                client_file_id=client_file_id,
                client_content_sha256=client_content_sha256,
                db=self.session,
            )
        return self.session.query(UploadJob).filter_by(job_id=response["job_id"]).one()

    def _run_worker(self, job: UploadJob) -> None:
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

    def _process(self, job: UploadJob) -> CommonFile:
        self._run_worker(job)
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

    def test_mobile_duplicate_fails_and_preserves_existing_legacy_original(self) -> None:
        content = self._jpeg_bytes("purple")
        original_job = self._upload(content)
        original_file = self._process(original_job)
        original_path = original_file.original_path
        original_state = self.session.get(MemoryKeeperFileState, original_file.id)
        state_revision = original_state.revision
        history_count = self.session.query(CommonMetadataHistory).count()

        mobile_job = self._upload(content, upload_source="mobile")
        incoming_path = self.storage.resolve_storage_path(mobile_job.incoming_path)
        self._run_worker(mobile_job)

        self.assertEqual(mobile_job.status, "FAILED")
        self.assertTrue(mobile_job.error_message.startswith("DUPLICATE_CONTENT:"))
        self.assertEqual(
            UploadJobService(self.session).get_job(mobile_job.job_id).failure_code,
            "DUPLICATE_CONTENT",
        )
        self.assertEqual(self.session.query(CommonFile).count(), 1)
        self.session.refresh(original_file)
        self.assertEqual(original_file.original_path, original_path)
        self.session.refresh(original_state)
        self.assertEqual(original_state.revision, state_revision)
        self.assertEqual(
            self.session.query(CommonMetadataHistory).count(),
            history_count,
        )
        self.assertFalse(incoming_path.exists())
        self.assertFalse(self.storage.mobile_original_root.exists())

    def test_legacy_duplicate_still_reuses_existing_file_and_completes(self) -> None:
        content = self._jpeg_bytes("maroon")
        original_file = self._process(self._upload(content))

        duplicate_job = self._upload(content)
        duplicate_file = self._process(duplicate_job)

        self.assertEqual(duplicate_file.id, original_file.id)
        self.assertEqual(
            duplicate_file.original_path,
            original_file.original_path,
        )
        self.assertIn("DUPLICATE_FOUND", duplicate_job.processing_log)

    def test_mobile_client_hash_mismatch_fails_before_storage(self) -> None:
        content = self._jpeg_bytes("yellow")
        job = self._upload(
            content,
            upload_source="mobile",
            client_content_sha256="0" * 64,
        )
        incoming_path = self.storage.resolve_storage_path(job.incoming_path)

        self._run_worker(job)

        self.assertEqual(job.status, "FAILED")
        self.assertTrue(job.error_message.startswith("CONTENT_SHA256_MISMATCH:"))
        self.assertEqual(
            UploadJobService(self.session).get_job(job.job_id).failure_code,
            "CONTENT_SHA256_MISMATCH",
        )
        self.assertEqual(self.session.query(CommonFile).count(), 0)
        self.assertFalse(incoming_path.exists())
        self.assertFalse(self.storage.mobile_original_root.exists())

    def test_controlled_mobile_rejection_does_not_write_failure_history(self) -> None:
        common_file = CommonFile(
            file_id="9" * 64,
            original_name="existing.jpg",
            original_path="original/existing.jpg",
        )
        self.session.add(common_file)
        self.session.commit()
        plugin = MagicMock()
        plugin.priority.return_value = 30
        plugin.plugin_name = "StoragePlugin"
        plugin.plugin_version = "1.0.0"
        plugin.worker_scope = "upload"

        def reject(context: PluginContext) -> None:
            context.common_file = common_file
            raise UploadValidationError(
                UploadFailureCode.DUPLICATE_CONTENT,
                "The file is already registered in MemoryKeeper.",
            )

        plugin.run.side_effect = reject
        context = PluginContext(db=self.session, storage_service=self.storage)

        with self.assertRaises(UploadValidationError):
            PluginManager([plugin]).run(context)

        self.assertEqual(self.session.query(CommonMetadataHistory).count(), 0)

    def test_mobile_matching_client_hash_succeeds(self) -> None:
        content = self._jpeg_bytes("white")
        job = self._upload(
            content,
            upload_source="mobile",
            client_content_sha256=hashlib.sha256(content).hexdigest(),
        )

        common_file = self._process(job)

        self.assertTrue(common_file.original_path.startswith("mobile-original/"))

    def test_astro_only_duplicate_becomes_explicit_memorykeeper_link(self) -> None:
        content = self._jpeg_bytes("teal")
        astro_job = self._upload(content, service_name="AstroJournal")
        common_file = self._process(astro_job)
        original_path = common_file.original_path

        mobile_job = self._upload(content, upload_source="mobile")
        reused = self._process(mobile_job)

        self.assertEqual(reused.id, common_file.id)
        self.assertEqual(reused.original_path, original_path)
        links = {
            row.service_name
            for row in self.session.query(CommonFileService).filter_by(
                file_id=common_file.id
            )
        }
        self.assertEqual(links, {"AstroJournal", "MemoryKeeper"})
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
