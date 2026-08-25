from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.astrojournal.models.observation_record import ObservationRecord
from app.astrojournal.routers.reset import execute_astrojournal_reset
from app.astrojournal.schemas.reset import AstroJournalResetExecuteRequest
from app.astrojournal.services.file_cleanup_service import (
    AstroJournalFileCleanupService,
    ResetAssetCleanupResult,
)
from app.astrojournal.services.plate_solve_service import PlateSolveService
from app.astrojournal.services.reset_service import AstroJournalResetService
from app.common.database import Base
from app.common.models.change_event import CommonChangeEvent
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.file_tag import CommonFileTag
from app.common.models.upload_job import UploadJob
from app.common.models.vision_job import CommonVisionJob
from app.common.repositories.upload_job_repository import UploadJobStatus
from app.common.repositories.vision_job_repository import VisionJobStatus
from app.common.services.api_clients.base_client import ApiClientError
from app.common.services.storage_service import StorageService
from app.main import app
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.photo import Photo
from worker.plugins.base import PluginContext
from worker.plugins.hash_plugin import HashPlugin
from worker.plugins.preview_plugin import PreviewPlugin
from worker.plugins.storage_plugin import StoragePlugin


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
    def preview_root(self) -> Path:
        return self.root / "preview"

    @property
    def thumb_root(self) -> Path:
        return self.root / "thumb"


class FailingResetCleanup(AstroJournalFileCleanupService):
    def delete_reset_assets(
        self,
        common_files: list[CommonFile],
    ) -> ResetAssetCleanupResult:
        return ResetAssetCleanupResult(
            succeeded=False,
            failed_file_id=common_files[0].id if common_files else None,
        )


class AstroJournalResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = LocalStorageService(Path(self.temp.name) / "PhotoPlatform")
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.counter = 0

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()
        self.temp.cleanup()

    def _create_file(
        self,
        *services: str,
        with_record: bool = True,
    ) -> tuple[CommonFile, dict[str, Path]]:
        self.counter += 1
        digest = f"{self.counter:064x}"
        paths = {
            "original": self.storage.original_root / "AstroJournal" / f"{digest}.jpg",
            "preview": self.storage.preview_root / digest[:2] / f"{digest}.jpg",
            "thumb": self.storage.thumb_root / digest[:2] / f"{digest}.jpg",
        }
        for kind, path in paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(kind.encode("ascii"))
        common_file = CommonFile(
            file_id=digest,
            original_name=f"{digest}.jpg",
            original_path=self.storage.to_relative_path(paths["original"]),
            preview_path=self.storage.to_relative_path(paths["preview"]),
            thumb_path=self.storage.to_relative_path(paths["thumb"]),
            service_name=services[0] if services else "AstroJournal",
            deleted=False,
        )
        self.session.add(common_file)
        self.session.flush()
        for service_name in services:
            self.session.add(
                CommonFileService(
                    file_id=common_file.id,
                    service_name=service_name,
                )
            )
        if with_record and "AstroJournal" in services:
            self.session.add(
                ObservationRecord(
                    file_id=common_file.id,
                    service_name="AstroJournal",
                    captured_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
                )
            )
        self.session.commit()
        return common_file, paths

    def _service(self) -> AstroJournalResetService:
        cleanup = AstroJournalFileCleanupService(
            self.session,
            storage_service=self.storage,
        )
        return AstroJournalResetService(
            self.session,
            cleanup_service=cleanup,
            storage_service=self.storage,
        )

    def _add_upload(self, service_name: str, status: str, client_id: str) -> UploadJob:
        path = self.storage.incoming_root / f"{client_id}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(client_id.encode("ascii"))
        job = UploadJob(
            job_id=f"00000000-0000-0000-0000-{self.counter:012d}",
            source_type="UPLOAD",
            status=status,
            incoming_path=self.storage.to_relative_path(path),
            service_name=service_name,
            client_file_id=client_id,
        )
        self.counter += 1
        self.session.add(job)
        self.session.commit()
        return job

    def test_preview_is_read_only_and_classifies_ownership(self) -> None:
        astro_only, astro_paths = self._create_file("AstroJournal")
        shared, shared_paths = self._create_file("AstroJournal", "MemoryKeeper")
        memorykeeper, memorykeeper_paths = self._create_file(
            "MemoryKeeper",
            with_record=False,
        )
        self._add_upload("AstroJournal", UploadJobStatus.WAITING, "astro-waiting")
        before = {
            "records": self.session.query(ObservationRecord).count(),
            "links": self.session.query(CommonFileService).count(),
            "jobs": self.session.query(UploadJob).count(),
        }

        preview = self._service().preview()

        self.assertEqual(preview.observation_record_count, 2)
        self.assertEqual(preview.astro_file_count, 2)
        self.assertEqual(preview.astro_only_file_count, 1)
        self.assertEqual(preview.shared_file_count, 1)
        self.assertEqual(preview.preserved_shared_file_count, 1)
        self.assertEqual(preview.pending_upload_count, 1)
        self.assertEqual(preview.physical_original_delete_count, 1)
        self.assertEqual(preview.physical_preview_delete_count, 1)
        self.assertEqual(preview.physical_thumbnail_delete_count, 1)
        self.assertEqual(preview.plate_solve_result_count, 0)
        self.assertEqual(preview.photo_object_count, 0)
        self.assertFalse(preview.reset_blocked)
        self.assertEqual(
            {
                "records": self.session.query(ObservationRecord).count(),
                "links": self.session.query(CommonFileService).count(),
                "jobs": self.session.query(UploadJob).count(),
            },
            before,
        )
        self.assertTrue(all(path.exists() for path in astro_paths.values()))
        self.assertTrue(all(path.exists() for path in shared_paths.values()))
        self.assertTrue(all(path.exists() for path in memorykeeper_paths.values()))
        self.assertFalse(memorykeeper.deleted)
        self.assertFalse(shared.deleted)
        self.assertFalse(astro_only.deleted)

    def test_execute_removes_astro_data_and_preserves_memorykeeper(self) -> None:
        astro_only, astro_paths = self._create_file("AstroJournal")
        shared, shared_paths = self._create_file("AstroJournal", "MemoryKeeper")
        memorykeeper, memorykeeper_paths = self._create_file(
            "MemoryKeeper",
            with_record=False,
        )
        for common_file in (astro_only, shared, memorykeeper):
            self.session.add_all(
                [
                    CommonFileMetadata(file_id=common_file.id, camera_make="kept"),
                    CommonFileTag(
                        file_id=common_file.id,
                        tag="raw",
                        tag_type="AI",
                        source="AI",
                        deleted=False,
                    ),
                ]
            )
        shared_state = MemoryKeeperFileState(
            file_id=shared.id,
            favorite=True,
            memo="shared memo",
        )
        memorykeeper_state = MemoryKeeperFileState(
            file_id=memorykeeper.id,
            favorite=True,
            memo="memorykeeper memo",
        )
        legacy_photo = Photo(file_path="kept", file_name="kept.jpg")
        self.session.add_all(
            [
                shared_state,
                memorykeeper_state,
                legacy_photo,
                CommonVisionJob(
                    file_id=astro_only.id,
                    status=VisionJobStatus.WAITING,
                    vision_provider="GOOGLE",
                    deleted=False,
                ),
                CommonVisionJob(
                    file_id=shared.id,
                    status=VisionJobStatus.COMPLETED,
                    vision_provider="GOOGLE",
                    deleted=False,
                ),
                CommonChangeEvent(
                    service_name="MemoryKeeper",
                    resource_type="Keep",
                    resource_id="1",
                    operation="UPDATE",
                    revision=1,
                    tombstone=False,
                ),
                CommonChangeEvent(
                    service_name="AstroJournal",
                    resource_type="ObservationRecord",
                    resource_id="old",
                    operation="CREATE",
                    revision=1,
                    tombstone=False,
                ),
            ]
        )
        self.session.commit()
        old_max_cursor = max(
            row.id for row in self.session.query(CommonChangeEvent).all()
        )
        astro_job = self._add_upload(
            "AstroJournal",
            UploadJobStatus.FAILED,
            "same-client-id",
        )
        memorykeeper_job = self._add_upload(
            "MemoryKeeper",
            UploadJobStatus.WAITING,
            "mk-client-id",
        )
        astro_incoming = self.storage.resolve_storage_path(astro_job.incoming_path)
        memorykeeper_incoming = self.storage.resolve_storage_path(
            memorykeeper_job.incoming_path
        )

        result = self._service().execute()

        self.assertTrue(result.reset_completed)
        self.assertEqual(result.deleted_observation_record_count, 2)
        self.assertEqual(result.removed_astro_file_link_count, 2)
        self.assertEqual(result.tombstoned_common_file_count, 1)
        self.assertEqual(result.preserved_shared_file_count, 1)
        self.assertEqual(result.deleted_upload_job_count, 1)
        self.assertTrue(all(not path.exists() for path in astro_paths.values()))
        self.assertTrue(all(path.exists() for path in shared_paths.values()))
        self.assertTrue(all(path.exists() for path in memorykeeper_paths.values()))
        self.assertFalse(astro_incoming.exists())
        self.assertTrue(memorykeeper_incoming.exists())
        self.session.refresh(astro_only)
        self.session.refresh(shared)
        self.session.refresh(memorykeeper)
        self.assertTrue(astro_only.deleted)
        self.assertFalse(shared.deleted)
        self.assertFalse(memorykeeper.deleted)
        links = {
            (row.file_id, row.service_name)
            for row in self.session.query(CommonFileService).all()
        }
        self.assertNotIn((astro_only.id, "AstroJournal"), links)
        self.assertIn((shared.id, "MemoryKeeper"), links)
        self.assertIn((memorykeeper.id, "MemoryKeeper"), links)
        self.assertEqual(self.session.query(ObservationRecord).count(), 0)
        self.assertIsNone(
            self.session.query(CommonFileMetadata)
            .filter_by(file_id=astro_only.id)
            .first()
        )
        self.assertIsNotNone(
            self.session.query(CommonFileMetadata).filter_by(file_id=shared.id).one()
        )
        self.assertEqual(self.session.get(MemoryKeeperFileState, shared.id).memo, "shared memo")
        self.assertEqual(
            self.session.get(MemoryKeeperFileState, memorykeeper.id).memo,
            "memorykeeper memo",
        )
        self.assertEqual(self.session.query(Photo).one().file_name, "kept.jpg")
        self.assertEqual(
            self.session.query(UploadJob)
            .filter_by(service_name="AstroJournal")
            .count(),
            0,
        )
        self.assertEqual(
            self.session.query(UploadJob)
            .filter_by(service_name="MemoryKeeper")
            .count(),
            1,
        )
        astro_events = self.session.query(CommonChangeEvent).filter_by(
            service_name="AstroJournal"
        ).order_by(CommonChangeEvent.id.asc()).all()
        self.assertEqual(len(astro_events), 2)
        self.assertEqual(astro_events[-1].resource_type, "AstroJournalReset")
        self.assertGreater(astro_events[-1].id, old_max_cursor)
        self.assertEqual(
            self.session.query(CommonChangeEvent)
            .filter_by(service_name="MemoryKeeper")
            .count(),
            1,
        )
        with self.assertRaises(ApiClientError):
            PlateSolveService(self.session)._get_astro_file(shared.id)

        # The old Astro idempotency row is gone, so the same client ID is valid.
        replay = UploadJob(
            job_id="99999999-0000-0000-0000-000000000999",
            source_type="UPLOAD",
            status=UploadJobStatus.WAITING,
            incoming_path="incoming/replay.jpg",
            service_name="AstroJournal",
            client_file_id="same-client-id",
        )
        self.session.add(replay)
        self.session.commit()
        self.assertIsNotNone(replay.id)

    def test_processing_upload_or_astro_only_vision_blocks_reset(self) -> None:
        common_file, paths = self._create_file("AstroJournal")
        processing = self._add_upload(
            "AstroJournal",
            UploadJobStatus.PROCESSING,
            "processing",
        )

        with self.assertRaises(HTTPException) as upload_error:
            self._service().execute()
        self.assertEqual(upload_error.exception.status_code, 409)
        self.assertEqual(self.session.query(ObservationRecord).count(), 1)
        self.assertTrue(all(path.exists() for path in paths.values()))

        self.session.delete(processing)
        self.session.add(
            CommonVisionJob(
                file_id=common_file.id,
                status=VisionJobStatus.PROCESSING,
                vision_provider="GOOGLE",
                deleted=False,
            )
        )
        self.session.commit()
        preview = self._service().preview()
        self.assertTrue(preview.reset_blocked)
        self.assertEqual(preview.blocked_reason, "PROCESSING_JOBS")
        with self.assertRaises(HTTPException) as vision_error:
            self._service().execute()
        self.assertEqual(vision_error.exception.status_code, 409)
        self.assertTrue(all(path.exists() for path in paths.values()))

    def test_cleanup_failure_rolls_back_database_state(self) -> None:
        common_file, paths = self._create_file("AstroJournal")
        job = self._add_upload(
            "AstroJournal",
            UploadJobStatus.FAILED,
            "failed-cleanup",
        )
        cleanup = FailingResetCleanup(
            self.session,
            storage_service=self.storage,
        )
        service = AstroJournalResetService(
            self.session,
            cleanup_service=cleanup,
            storage_service=self.storage,
        )

        with self.assertRaises(HTTPException) as error:
            service.execute()

        self.assertEqual(error.exception.status_code, 500)
        self.assertEqual(self.session.query(ObservationRecord).count(), 1)
        self.assertEqual(
            self.session.query(CommonFileService)
            .filter_by(file_id=common_file.id, service_name="AstroJournal")
            .count(),
            1,
        )
        self.assertIsNotNone(self.session.get(UploadJob, job.id))
        self.assertTrue(all(path.exists() for path in paths.values()))

    def test_reset_allows_byte_identical_asset_restore(self) -> None:
        seed = Path(self.temp.name) / "seed.jpg"
        Image.new("RGB", (24, 16), color="navy").save(seed, format="JPEG")
        content = seed.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        paths = {
            "original": self.storage.original_root / "AstroJournal" / f"{digest}.jpg",
            "preview": self.storage.preview_root / digest[:2] / f"{digest}.jpg",
            "thumb": self.storage.thumb_root / digest[:2] / f"{digest}.jpg",
        }
        for path in paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        common_file = CommonFile(
            file_id=digest,
            original_name="same.jpg",
            extension=".jpg",
            mime_type="image/jpeg",
            original_path=self.storage.to_relative_path(paths["original"]),
            preview_path=self.storage.to_relative_path(paths["preview"]),
            thumb_path=self.storage.to_relative_path(paths["thumb"]),
            service_name="AstroJournal",
            deleted=False,
        )
        self.session.add(common_file)
        self.session.flush()
        original_id = common_file.id
        self.session.add_all(
            [
                CommonFileService(
                    file_id=common_file.id,
                    service_name="AstroJournal",
                ),
                ObservationRecord(
                    file_id=common_file.id,
                    service_name="AstroJournal",
                    captured_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
                ),
            ]
        )
        self.session.commit()

        self._service().execute()

        incoming = self.storage.incoming_root / "same.jpg"
        incoming.parent.mkdir(parents=True, exist_ok=True)
        incoming.write_bytes(content)
        job = UploadJob(
            job_id="77777777-0000-0000-0000-000000000777",
            source_type="UPLOAD",
            status=UploadJobStatus.PROCESSING,
            incoming_path=self.storage.to_relative_path(incoming),
            service_name="AstroJournal",
        )
        self.session.add(job)
        self.session.commit()
        context = PluginContext(
            db=self.session,
            storage_service=self.storage,
            incoming_path=incoming,
            job=job,
            original_name="same.jpg",
            extension=".jpg",
            mime_type="image/jpeg",
            service_name="AstroJournal",
            metadata={
                "observation_date": "2026-08-20",
                "canonical_target_id": "M42",
            },
        )
        HashPlugin().run(context)
        self.assertTrue(context.restore_deleted_common_file)
        PreviewPlugin().run(context)
        StoragePlugin().run(context)

        self.session.refresh(common_file)
        self.assertEqual(common_file.id, original_id)
        self.assertFalse(common_file.deleted)
        self.assertEqual(
            self.session.query(CommonFileService)
            .filter_by(file_id=common_file.id, service_name="AstroJournal")
            .count(),
            1,
        )
        self.assertTrue(
            self.storage.resolve_storage_path(common_file.original_path).exists()
        )

    def test_api_confirmation_and_bearer_protection(self) -> None:
        with self.assertRaises(ValidationError):
            AstroJournalResetExecuteRequest(confirmation="WRONG")
        payload = AstroJournalResetExecuteRequest(
            confirmation="RESET_ASTROJOURNAL"
        )
        response = execute_astrojournal_reset(payload, self.session)
        self.assertTrue(response.reset_completed)

        schema = app.openapi()
        reset_routes = {
            path: schema["paths"][path]["post"]
            for path in schema["paths"]
            if path.startswith("/api/astro/reset")
        }
        self.assertEqual(
            set(reset_routes),
            {"/api/astro/reset/preview", "/api/astro/reset/execute"},
        )
        for operation in reset_routes.values():
            self.assertEqual(operation["security"], [{"TCBackendBearer": []}])

    def test_large_record_set_uses_bulk_database_operations(self) -> None:
        for _ in range(5):
            common_file, _ = self._create_file("AstroJournal")
            for _record in range(9):
                self.session.add(
                    ObservationRecord(
                        file_id=common_file.id,
                        service_name="AstroJournal",
                        captured_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
                    )
                )
        self.session.commit()
        statement_count = 0

        def count_statement(*_args):
            nonlocal statement_count
            statement_count += 1

        event.listen(self.engine, "before_cursor_execute", count_statement)
        try:
            result = self._service().execute()
        finally:
            event.remove(self.engine, "before_cursor_execute", count_statement)

        self.assertEqual(result.deleted_observation_record_count, 50)
        self.assertLess(statement_count, 60)


if __name__ == "__main__":
    unittest.main()
