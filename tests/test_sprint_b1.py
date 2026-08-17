from __future__ import annotations

import io
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.database import Base
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.upload_job import UploadJob
from app.common.repositories.upload_job_repository import UploadJobRepository
from app.common.routers.capabilities import capabilities
from app.common.routers.upload import (
    _idempotent_response,
    _validate_service_name,
    upload_file,
)
from app.common.schema_sync import initialize_database
from app.common.services.gallery_service import GalleryService
from app.common.services.upload_metadata import decode_upload_metadata
from app.common.services.upload_job_service import UploadJobService
from worker import background_worker


class FakeStorageService:
    def __init__(self) -> None:
        self.saved = 0

    def save_incoming(self, file: UploadFile, job_id: str) -> str:
        self.saved += 1
        return f"incoming/{job_id}_{file.filename}"

    def delete_incoming(self, incoming_path: str) -> None:
        return None

    def resolve_storage_path(self, incoming_path: str) -> Path:
        return Path(incoming_path)


class SprintB1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    @staticmethod
    def _upload_file() -> UploadFile:
        return UploadFile(filename="photo.jpg", file=io.BytesIO(b"image"))

    def test_legacy_file_only_upload_response_is_unchanged(self) -> None:
        fake_storage = FakeStorageService()
        with patch("app.common.routers.upload.storage_service", fake_storage):
            response = upload_file(
                file=self._upload_file(),
                service_name="MemoryKeeper",
                client_file_id=None,
                client_content_sha256=None,
                db=self.session,
            )
        self.assertEqual(set(response), {"id", "job_id", "status", "incoming_path"})
        self.assertEqual(response["status"], "WAITING")

    def test_astro_upload_and_idempotent_replay(self) -> None:
        fake_storage = FakeStorageService()
        digest = "a" * 64
        with patch("app.common.routers.upload.storage_service", fake_storage):
            created = upload_file(
                file=self._upload_file(),
                service_name="AstroJournal",
                client_file_id="astro-client-1",
                client_content_sha256=digest,
                observation_date=date(2026, 8, 17),
                canonical_target_id="M31",
                target_display_name="Andromeda Galaxy",
                db=self.session,
            )
            replay = upload_file(
                file=self._upload_file(),
                service_name="AstroJournal",
                client_file_id="astro-client-1",
                client_content_sha256=digest,
                observation_date=date(2025, 1, 1),
                canonical_target_id="M42",
                target_display_name="Orion Nebula",
                db=self.session,
            )
        self.assertEqual(created["service_name"], "AstroJournal")
        self.assertFalse(created["idempotent_replay"])
        self.assertIsNone(created["common_file_id"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertIsNone(replay["common_file_id"])
        self.assertEqual(replay["job_id"], created["job_id"])
        self.assertEqual(fake_storage.saved, 1)
        job = self.session.query(UploadJob).filter_by(job_id=created["job_id"]).one()
        self.assertEqual(
            decode_upload_metadata(job.processing_log),
            {
                "observation_date": "2026-08-17",
                "canonical_target_id": "M31",
                "target_display_name": "Andromeda Galaxy",
            },
        )

    def test_completed_job_status_resolves_common_file_primary_key(self) -> None:
        digest = "d" * 64
        common_file = CommonFile(file_id=digest, original_name="astro.jpg")
        job = UploadJob(
            job_id="44444444-4444-4444-4444-444444444444",
            source_type="UPLOAD",
            status="COMPLETED",
            incoming_path="incoming/astro.jpg",
            service_name="AstroJournal",
            file_id=digest,
        )
        self.session.add_all([common_file, job])
        self.session.commit()

        response = UploadJobService(self.session).get_job(job.job_id)

        self.assertEqual(response.backend_file_id, digest)
        self.assertIsInstance(response.common_file_id, int)
        self.assertEqual(response.common_file_id, common_file.id)

    def test_waiting_and_failed_job_status_allow_missing_common_file(self) -> None:
        waiting = UploadJob(
            job_id="55555555-5555-5555-5555-555555555555",
            source_type="UPLOAD",
            status="WAITING",
            incoming_path="incoming/waiting.jpg",
        )
        failed = UploadJob(
            job_id="66666666-6666-6666-6666-666666666666",
            source_type="UPLOAD",
            status="FAILED",
            incoming_path="incoming/failed.jpg",
        )
        self.session.add_all([waiting, failed])
        self.session.commit()

        service = UploadJobService(self.session)
        self.assertIsNone(service.get_job(waiting.job_id).common_file_id)
        self.assertIsNone(service.get_job(failed.job_id).common_file_id)

    def test_completed_idempotent_replay_returns_both_file_identifiers(self) -> None:
        digest = "e" * 64
        common_file = CommonFile(file_id=digest, original_name="completed.jpg")
        job = UploadJob(
            job_id="77777777-7777-7777-7777-777777777777",
            source_type="UPLOAD",
            status="COMPLETED",
            incoming_path="incoming/completed.jpg",
            service_name="AstroJournal",
            client_file_id="completed-client-id",
            client_content_sha256=digest,
            file_id=digest,
        )
        self.session.add_all([common_file, job])
        self.session.commit()

        response = _idempotent_response(
            UploadJobRepository(self.session),
            job,
            digest,
        )

        self.assertEqual(response["backend_file_id"], digest)
        self.assertEqual(response["common_file_id"], common_file.id)

    def test_idempotency_hash_conflict_returns_409(self) -> None:
        repository = UploadJobRepository(self.session)
        job = repository.create_waiting_job(
            job_id="11111111-1111-1111-1111-111111111111",
            source_type="UPLOAD",
            incoming_path="incoming/test.jpg",
            service_name="AstroJournal",
            client_file_id="astro-client-1",
            client_content_sha256="a" * 64,
        )
        with self.assertRaises(HTTPException) as raised:
            _idempotent_response(repository, job, "b" * 64)
        self.assertEqual(raised.exception.status_code, 409)

    def test_missing_hash_is_backfilled_on_replay(self) -> None:
        repository = UploadJobRepository(self.session)
        job = repository.create_waiting_job(
            job_id="33333333-3333-3333-3333-333333333333",
            source_type="UPLOAD",
            incoming_path="incoming/test.jpg",
            client_file_id="memory-client-1",
        )
        response = _idempotent_response(repository, job, "c" * 64)
        self.assertTrue(response["idempotent_replay"])
        self.assertEqual(job.client_content_sha256, "c" * 64)

    def test_invalid_service_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            _validate_service_name("NotSupported")
        self.assertEqual(raised.exception.status_code, 422)

    def test_schema_sync_is_idempotent_on_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(f"sqlite:///{Path(directory) / 'b1.db'}")
            try:
                initialize_database(engine)
                self.assertEqual(initialize_database(engine), [])
            finally:
                engine.dispose()

    def test_gallery_defaults_to_memorykeeper(self) -> None:
        memory = CommonFile(file_id="m" * 64, original_name="memory.jpg")
        astro = CommonFile(
            file_id="a" * 64,
            original_name="astro.jpg",
            service_name="AstroJournal",
        )
        self.session.add_all([memory, astro])
        self.session.flush()
        self.session.add_all(
            [
                CommonFileService(file_id=memory.id, service_name="MemoryKeeper"),
                CommonFileService(file_id=astro.id, service_name="AstroJournal"),
            ]
        )
        self.session.commit()
        response = GalleryService(self.session).list_gallery()
        self.assertEqual(response.total, 1)
        self.assertEqual(response.items[0].service_name, "MemoryKeeper")
        astro = GalleryService(self.session).list_gallery(service_name="AstroJournal")
        self.assertEqual(astro.total, 1)
        self.assertEqual(astro.items[0].service_name, "AstroJournal")

    def test_worker_uses_job_service_name(self) -> None:
        job = UploadJob(
            job_id="22222222-2222-2222-2222-222222222222",
            source_type="UPLOAD",
            status="PROCESSING",
            incoming_path="incoming/astro.jpg",
            service_name="AstroJournal",
        )
        self.session.add(job)
        self.session.commit()
        observed: dict[str, str] = {}

        class Pipeline:
            def run(self, context) -> None:
                observed["service_name"] = context.service_name
                context.common_file = SimpleNamespace(id=1, file_id="c" * 64)
                context.stop_pipeline = True

        with patch.object(background_worker, "StorageService", FakeStorageService), patch.object(
            background_worker.PluginManager,
            "load_plugins",
            return_value=Pipeline(),
        ):
            background_worker.process_upload_job(self.session, job, worker_id="test")
        self.assertEqual(observed["service_name"], "AstroJournal")

    def test_capabilities_contract(self) -> None:
        response = capabilities()
        self.assertEqual(response["api_version"], "1.1")
        self.assertEqual(response["supported_services"], ["MemoryKeeper", "AstroJournal"])
        self.assertTrue(response["upload_contract"]["supports_client_file_id"])
