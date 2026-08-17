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
        self.assertTrue(replay["idempotent_replay"])
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
