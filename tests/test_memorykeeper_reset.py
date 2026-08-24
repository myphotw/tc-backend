from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.astrojournal.models.observation_record import ObservationRecord
from app.common.config import settings
from app.common.database import Base
from app.common.models.change_event import CommonChangeEvent
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.file_tag import CommonFileTag
from app.common.models.upload_job import UploadJob
from app.common.models.vision_job import CommonVisionJob
from app.common.repositories.vision_job_repository import (
    VisionJobRepository,
    VisionJobStatus,
)
from app.common.security import require_backend_auth
from app.common.services.gallery_service import GalleryService
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.file_tag_suppression import (
    MemoryKeeperFileTagSuppression,
)
from app.memorykeeper.models.place import MemoryKeeperPlace
from app.memorykeeper.models.tag import Tag
from app.memorykeeper.models.tag_canonical_override import (
    MemoryKeeperTagCanonicalOverride,
)
from app.memorykeeper.routers import reset as reset_router
from app.memorykeeper.schemas.reset import MemoryKeeperResetExecuteRequest
from app.memorykeeper.services.pending_service import MemoryKeeperPendingService
from app.memorykeeper.services.reset_service import MemoryKeeperResetService
from worker.plugins.base import PluginContext
from worker.plugins.hash_plugin import HashPlugin


class _FakeStorage:
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.deleted_incoming: list[str] = []

    def calculate_sha256(self, _path: Path) -> str:
        return self.digest

    def delete_incoming(self, path: str) -> None:
        self.deleted_incoming.append(path)


class MemoryKeeperResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

        self.memory_only = self._file("1" * 64, ("MemoryKeeper",))
        self.shared = self._file(
            "2" * 64,
            ("MemoryKeeper", "AstroJournal"),
        )
        self.astro_only = self._file("3" * 64, ("AstroJournal",))

        self.place = MemoryKeeperPlace(
            display_name="Old Place",
            canonical_name="old place",
            latitude=37.5,
            longitude=127.0,
            radius_m=200,
        )
        self.db.add(self.place)
        self.db.flush()
        self.metadata = CommonFileMetadata(
            file_id=self.memory_only.id,
            camera_make="Preserved Camera",
            datetime_original=datetime(2024, 1, 2, tzinfo=timezone.utc),
            gps_lat=37.5,
            gps_lon=127.0,
            country="KR",
            province="Seoul",
            city="Seoul",
            district="Jongno",
            place_name="Raw Reverse Geocode",
            memorykeeper_place_id=self.place.id,
            place_match_source="USER",
            place_match_distance_m=12.5,
            place_match_revision=4,
            locked=True,
        )
        self.db.add(self.metadata)
        self.db.add(
            MemoryKeeperFileState(
                file_id=self.memory_only.id,
                favorite=True,
                memo="reset me",
                revision=3,
            )
        )
        self.tag = Tag(
            tag_name="Old Tag",
            normalized_name="old tag",
            tag_type="USER",
            source="USER",
            deleted=False,
        )
        self.db.add(self.tag)
        self.db.flush()
        self.db.add_all(
            [
                CommonFileTag(
                    file_id=self.memory_only.id,
                    memorykeeper_tag_id=self.tag.id,
                    tag="Old Tag",
                    tag_type="USER",
                    source="USER",
                    deleted=False,
                ),
                CommonFileTag(
                    file_id=self.shared.id,
                    tag="Legacy MemoryKeeper User Tag",
                    tag_type="USER",
                    source="USER",
                    deleted=False,
                ),
                CommonFileTag(
                    file_id=self.astro_only.id,
                    tag="Astro-only User Projection",
                    tag_type="USER",
                    source="USER",
                    deleted=False,
                ),
                CommonFileTag(
                    file_id=self.memory_only.id,
                    tag="Dog",
                    tag_type="AI",
                    source="AI",
                    confidence=91,
                    deleted=False,
                ),
                MemoryKeeperFileTagSuppression(
                    file_id=self.memory_only.id,
                    canonical_key="dog",
                    deleted=False,
                ),
                MemoryKeeperTagCanonicalOverride(
                    canonical_key="dog",
                    memorykeeper_tag_id=self.tag.id,
                    suppressed=False,
                ),
            ]
        )
        self.completed_vision = CommonVisionJob(
            file_id=self.memory_only.id,
            priority=10,
            status=VisionJobStatus.COMPLETED,
            retry_count=0,
            vision_provider="GOOGLE",
            completed_at=datetime.now(timezone.utc),
            deleted=False,
        )
        self.waiting_vision = CommonVisionJob(
            file_id=self.memory_only.id,
            priority=5,
            status=VisionJobStatus.WAITING,
            retry_count=0,
            vision_provider="GOOGLE",
            requested_at=datetime.now(timezone.utc),
            deleted=False,
        )
        self.astro_waiting_vision = CommonVisionJob(
            file_id=self.astro_only.id,
            priority=5,
            status=VisionJobStatus.WAITING,
            retry_count=0,
            vision_provider="GOOGLE",
            requested_at=datetime.now(timezone.utc),
            deleted=False,
        )
        self.shared_processing_vision = CommonVisionJob(
            file_id=self.shared.id,
            priority=50,
            status=VisionJobStatus.PROCESSING,
            retry_count=0,
            vision_provider="GOOGLE",
            requested_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            deleted=False,
        )
        self.shared_zero_label_completed = CommonVisionJob(
            file_id=self.shared.id,
            priority=20,
            status=VisionJobStatus.COMPLETED,
            retry_count=0,
            vision_provider="GOOGLE",
            completed_at=datetime.now(timezone.utc),
            deleted=False,
        )
        self.db.add_all(
            [
                self.completed_vision,
                self.waiting_vision,
                self.astro_waiting_vision,
                self.shared_processing_vision,
                self.shared_zero_label_completed,
                UploadJob(
                    job_id="10000000-0000-0000-0000-000000000001",
                    source_type="UPLOAD",
                    status="COMPLETED",
                    incoming_path="incoming/old-memory.jpg",
                    service_name="MemoryKeeper",
                    client_file_id="old-memory-client-id",
                    file_id=self.memory_only.file_id,
                ),
                UploadJob(
                    job_id="10000000-0000-0000-0000-000000000002",
                    source_type="UPLOAD",
                    status="COMPLETED",
                    incoming_path="incoming/old-astro.jpg",
                    service_name="AstroJournal",
                    client_file_id="old-astro-client-id",
                    file_id=self.astro_only.file_id,
                ),
                ObservationRecord(
                    file_id=self.shared.id,
                    service_name="AstroJournal",
                    catalog_object_id="M42",
                    captured_at=datetime.now(timezone.utc),
                    memo="preserve astro",
                ),
            ]
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def _file(self, digest: str, services: tuple[str, ...]) -> CommonFile:
        original = self.root / f"{digest}.jpg"
        preview = self.root / f"{digest}.preview.jpg"
        thumb = self.root / f"{digest}.thumb.jpg"
        original.write_bytes(b"original")
        preview.write_bytes(b"preview")
        thumb.write_bytes(b"thumb")
        item = CommonFile(
            file_id=digest,
            original_name=f"{digest}.jpg",
            original_path=str(original),
            preview_path=str(preview),
            thumb_path=str(thumb),
            service_name=services[0],
            deleted=False,
        )
        self.db.add(item)
        self.db.flush()
        for service in services:
            self.db.add(CommonFileService(file_id=item.id, service_name=service))
        return item

    def test_preview_is_read_only_and_reports_preserved_scope(self) -> None:
        before = self._table_counts()
        result = MemoryKeeperResetService(self.db).preview()
        after = self._table_counts()

        self.assertEqual(before, after)
        self.assertEqual(result.memorykeeper_file_count, 2)
        self.assertEqual(result.place_count, 1)
        self.assertEqual(result.user_tag_count, 1)
        self.assertEqual(result.favorite_count, 1)
        self.assertEqual(result.memo_count, 1)
        self.assertEqual(result.file_tag_relation_count, 2)
        self.assertEqual(result.file_tag_suppression_count, 1)
        self.assertEqual(result.preserved_common_file_count, 2)
        self.assertEqual(result.preserved_raw_vision_count, 2)
        self.assertEqual(result.shared_with_other_service_count, 1)
        self.assertFalse(result.reset_blocked)
        self.assertEqual(result.processing_vision_job_count, 0)

    def test_execute_resets_memorykeeper_and_preserves_assets_astro_and_raw(self) -> None:
        original_paths = {
            item.id: (item.original_path, item.preview_path, item.thumb_path)
            for item in (self.memory_only, self.shared, self.astro_only)
        }
        result = MemoryKeeperResetService(self.db).execute()
        self.db.expire_all()

        self.assertTrue(result.reset_completed)
        self.assertEqual(result.affected_file_count, 2)
        self.assertEqual(result.removed_place_count, 1)
        self.assertEqual(result.removed_user_tag_count, 1)
        self.assertEqual(result.cleared_state_count, 1)
        self.assertEqual(result.preserved_raw_vision_count, 2)
        self.assertEqual(GalleryService(self.db).list_gallery(service_name="MemoryKeeper").total, 0)
        self.assertEqual(MemoryKeeperPendingService(self.db).list(page=1, page_size=20).total, 0)
        self.assertEqual(
            self.db.query(CommonFileService)
            .filter(CommonFileService.service_name == "MemoryKeeper")
            .count(),
            0,
        )
        self.assertEqual(
            self.db.query(CommonFileService)
            .filter(CommonFileService.service_name == "AstroJournal")
            .count(),
            2,
        )
        self.assertEqual(self.db.query(CommonFile).count(), 3)
        self.assertEqual(self.db.query(ObservationRecord).count(), 1)
        self.assertEqual(self.db.query(MemoryKeeperPlace).count(), 0)
        self.assertEqual(self.db.query(Tag).count(), 0)
        self.assertEqual(self.db.query(MemoryKeeperFileState).count(), 0)
        self.assertEqual(self.db.query(MemoryKeeperFileTagSuppression).count(), 0)
        self.assertEqual(self.db.query(MemoryKeeperTagCanonicalOverride).count(), 0)
        self.assertEqual(
            self.db.query(CommonFileTag)
            .filter(CommonFileTag.memorykeeper_tag_id.is_not(None))
            .count(),
            0,
        )
        self.assertEqual(
            self.db.query(CommonFileTag)
            .filter(CommonFileTag.source == "USER")
            .all()[0].tag,
            "Astro-only User Projection",
        )
        raw = self.db.query(CommonFileTag).filter(CommonFileTag.source == "AI").one()
        self.assertEqual((raw.tag, raw.confidence, raw.deleted), ("Dog", 91, False))

        metadata = self.db.get(CommonFileMetadata, self.metadata.id)
        self.assertEqual(metadata.camera_make, "Preserved Camera")
        self.assertEqual((metadata.gps_lat, metadata.gps_lon), (37.5, 127.0))
        self.assertEqual(metadata.place_name, "Raw Reverse Geocode")
        self.assertEqual((metadata.country, metadata.province, metadata.city), ("KR", "Seoul", "Seoul"))
        self.assertIsNone(metadata.memorykeeper_place_id)
        self.assertIsNone(metadata.place_match_source)
        self.assertIsNone(metadata.place_match_distance_m)
        self.assertEqual(metadata.place_match_revision, 0)
        self.assertTrue(metadata.locked)

        completed = self.db.get(CommonVisionJob, self.completed_vision.id)
        waiting = self.db.get(CommonVisionJob, self.waiting_vision.id)
        astro_waiting = self.db.get(CommonVisionJob, self.astro_waiting_vision.id)
        shared_processing = self.db.get(
            CommonVisionJob,
            self.shared_processing_vision.id,
        )
        self.assertFalse(completed.deleted)
        self.assertTrue(waiting.deleted)
        self.assertFalse(astro_waiting.deleted)
        self.assertFalse(shared_processing.deleted)
        self.assertEqual(shared_processing.status, VisionJobStatus.PROCESSING)
        self.assertEqual(
            self.db.query(UploadJob).filter(UploadJob.service_name == "MemoryKeeper").count(),
            0,
        )
        self.assertEqual(
            self.db.query(UploadJob).filter(UploadJob.service_name == "AstroJournal").count(),
            1,
        )
        event = self.db.get(CommonChangeEvent, result.reset_event_cursor)
        self.assertEqual(event.resource_type, "MemoryKeeperReset")
        self.assertEqual(event.service_name, "MemoryKeeper")

        for file_id, paths in original_paths.items():
            item = self.db.get(CommonFile, file_id)
            self.assertFalse(item.deleted)
            self.assertEqual((item.original_path, item.preview_path, item.thumb_path), paths)
            for path in paths:
                self.assertTrue(Path(path).is_file())

    def test_execute_rolls_back_everything_when_commit_fails(self) -> None:
        with patch.object(self.db, "commit", side_effect=RuntimeError("forced")):
            with self.assertRaisesRegex(RuntimeError, "forced"):
                MemoryKeeperResetService(self.db).execute()
        self.db.expire_all()

        self.assertEqual(
            self.db.query(CommonFileService)
            .filter(CommonFileService.service_name == "MemoryKeeper")
            .count(),
            2,
        )
        self.assertEqual(self.db.query(MemoryKeeperPlace).count(), 1)
        self.assertEqual(self.db.query(Tag).count(), 1)
        self.assertEqual(self.db.query(MemoryKeeperFileState).count(), 1)
        self.assertEqual(
            self.db.query(CommonChangeEvent)
            .filter(CommonChangeEvent.resource_type == "MemoryKeeperReset")
            .count(),
            0,
        )

    def test_active_upload_and_memorykeeper_only_processing_vision_block_reset(self) -> None:
        upload = UploadJob(
            job_id="10000000-0000-0000-0000-000000000003",
            source_type="UPLOAD",
            status="PROCESSING",
            incoming_path="incoming/active.jpg",
            service_name="MemoryKeeper",
        )
        self.db.add(upload)
        self.db.commit()
        with self.assertRaises(HTTPException) as upload_error:
            MemoryKeeperResetService(self.db).execute()
        self.assertEqual(upload_error.exception.status_code, 409)
        self.db.delete(upload)
        self.db.commit()

        processing = CommonVisionJob(
            file_id=self.memory_only.id,
            priority=99,
            status=VisionJobStatus.PROCESSING,
            retry_count=0,
            vision_provider="GOOGLE",
            deleted=False,
        )
        self.db.add(processing)
        self.db.commit()
        with self.assertRaises(HTTPException) as vision_error:
            MemoryKeeperResetService(self.db).execute()
        self.assertEqual(vision_error.exception.status_code, 409)
        self.assertEqual(
            vision_error.exception.detail["code"],
            "MEMORYKEEPER_RESET_BLOCKED",
        )
        self.assertEqual(self.db.query(CommonFileService).count(), 4)

    def test_stale_waiting_vision_claim_cannot_cross_reset(self) -> None:
        stale_job = self.waiting_vision
        MemoryKeeperResetService(self.db).execute()
        claimed = VisionJobRepository(self.db).mark_processing(stale_job)
        self.assertIsNone(claimed)
        self.db.expire_all()
        self.assertTrue(self.db.get(CommonVisionJob, stale_job.id).deleted)

    def test_same_sha_reimport_reuses_completed_raw_or_requeues_missing_result(self) -> None:
        missing = self._file("4" * 64, ("MemoryKeeper",))
        old_waiting = CommonVisionJob(
            file_id=missing.id,
            priority=1,
            status=VisionJobStatus.WAITING,
            retry_count=0,
            vision_provider="GOOGLE",
            deleted=False,
        )
        self.db.add(old_waiting)
        self.db.commit()
        MemoryKeeperResetService(self.db).execute()

        self.memory_only.favorite = True
        self.db.commit()
        reused_context = self._hash_context(self.memory_only.file_id)
        HashPlugin().run(reused_context)
        self.assertIn("VISION_RAW_REUSED:COMPLETED", reused_context.processing_log)
        self.assertEqual(
            self.db.query(CommonVisionJob)
            .filter(CommonVisionJob.file_id == self.memory_only.id)
            .filter(CommonVisionJob.deleted.is_(False))
            .count(),
            1,
        )
        reused_state = self.db.get(MemoryKeeperFileState, self.memory_only.id)
        self.assertFalse(reused_state.favorite)
        self.assertIsNone(reused_state.memo)

        missing_context = self._hash_context(missing.file_id)
        HashPlugin().run(missing_context)
        self.assertIn("VISION_QUEUE_CREATED:RESET_REIMPORT", missing_context.processing_log)
        active = (
            self.db.query(CommonVisionJob)
            .filter(CommonVisionJob.file_id == missing.id)
            .filter(CommonVisionJob.deleted.is_(False))
            .all()
        )
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].status, VisionJobStatus.WAITING)
        self.assertEqual(active[0].retry_count, 0)

    def test_reset_routes_require_bearer_and_literal_confirmation(self) -> None:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/memorykeeper/reset/execute",
                "headers": [],
                "client": ("127.0.0.1", 1234),
                "scheme": "http",
                "server": ("testserver", 80),
                "query_string": b"",
            }
        )
        with patch.object(settings, "TC_BACKEND_AUTH_TOKEN", "reset-token"):
            with self.assertRaises(HTTPException) as missing:
                require_backend_auth(request, None)
            self.assertEqual(missing.exception.status_code, 401)
            authorized_request = Request(
                {
                    **request.scope,
                    "headers": [
                        (b"authorization", b"Bearer reset-token")
                    ],
                }
            )
            require_backend_auth(
                authorized_request,
                HTTPAuthorizationCredentials(
                    scheme="Bearer",
                    credentials="reset-token",
                ),
            )

        with self.assertRaises(ValidationError):
            MemoryKeeperResetExecuteRequest(confirmation="WRONG")
        preview = reset_router.preview_memorykeeper_reset(db=self.db)
        self.assertEqual(preview.memorykeeper_file_count, 2)
        execute = reset_router.execute_memorykeeper_reset(
            payload=MemoryKeeperResetExecuteRequest(
                confirmation="RESET_MEMORYKEEPER"
            ),
            db=self.db,
        )
        self.assertTrue(execute.reset_completed)

        from app.main import app as main_app

        schema = main_app.openapi()
        for path in (
            "/api/memorykeeper/reset/preview",
            "/api/memorykeeper/reset/execute",
        ):
            self.assertIn(path, schema["paths"])
            self.assertEqual(
                schema["paths"][path]["post"]["security"],
                [{"TCBackendBearer": []}],
            )

    def _hash_context(self, digest: str) -> PluginContext:
        incoming = self.root / f"incoming-{digest}.jpg"
        incoming.write_bytes(b"same content")
        return PluginContext(
            db=self.db,
            storage_service=_FakeStorage(digest),
            incoming_path=incoming,
            job=UploadJob(
                job_id=f"20000000-0000-0000-0000-{digest[:12]}",
                source_type="UPLOAD",
                status="PROCESSING",
                incoming_path=str(incoming),
                service_name="MemoryKeeper",
            ),
            service_name="MemoryKeeper",
        )

    def _table_counts(self) -> tuple[int, ...]:
        return (
            self.db.query(CommonFile).count(),
            self.db.query(CommonFileService).count(),
            self.db.query(CommonFileMetadata).count(),
            self.db.query(CommonFileTag).count(),
            self.db.query(MemoryKeeperPlace).count(),
            self.db.query(Tag).count(),
            self.db.query(MemoryKeeperFileState).count(),
            self.db.query(CommonVisionJob).count(),
            self.db.query(UploadJob).count(),
            self.db.query(ObservationRecord).count(),
            self.db.query(CommonChangeEvent).count(),
        )


if __name__ == "__main__":
    unittest.main()
