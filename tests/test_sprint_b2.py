from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.database import Base
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.upload_job import UploadJob
from app.common.repositories.file_service_repository import FileServiceRepository
from app.common.schema_sync import initialize_database
from app.common.services.gallery_service import GalleryService
from worker.plugins.base import PluginContext
from worker.plugins.hash_plugin import HashPlugin
from worker.plugins.storage_plugin import StoragePlugin


class FakeStorageService:
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.deleted_paths: list[str] = []

    def calculate_sha256(self, path: Path) -> str:
        return self.digest

    def delete_incoming(self, incoming_path: str) -> None:
        self.deleted_paths.append(incoming_path)

    def move_to_storage(self, **kwargs) -> Path:
        return Path("PhotoPlatform/original") / f"{kwargs['file_id']}{kwargs['extension']}"

    @staticmethod
    def to_relative_path(path: Path | None) -> str | None:
        return str(path).replace("\\", "/") if path is not None else None


class SprintB2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _file(self, file_id: str, service_name: str = "MemoryKeeper") -> CommonFile:
        file = CommonFile(
            file_id=file_id,
            original_name=f"{file_id[:8]}.jpg",
            service_name=service_name,
        )
        self.session.add(file)
        self.session.flush()
        return file

    def test_new_memorykeeper_upload_creates_file_and_link(self) -> None:
        digest = "1" * 64
        with tempfile.TemporaryDirectory() as directory:
            incoming = Path(directory) / "new.jpg"
            incoming.write_bytes(b"new bytes")
            context = PluginContext(
                db=self.session,
                storage_service=FakeStorageService(digest),
                incoming_path=incoming,
                job=UploadJob(
                    job_id="b2000000-0000-0000-0000-000000000003",
                    source_type="UPLOAD",
                    status="PROCESSING",
                    incoming_path="incoming/new.jpg",
                ),
                file_id=digest,
                original_name="new.jpg",
                extension=".jpg",
                file_size=incoming.stat().st_size,
            )
            StoragePlugin().run(context)

        self.assertIsNotNone(context.common_file)
        file = context.common_file
        self.assertEqual(self.session.query(CommonFile).count(), 1)
        link = (
            self.session.query(CommonFileService)
            .filter(CommonFileService.file_id == file.id)
            .one()
        )
        self.assertEqual(link.file_id, file.id)
        self.assertEqual(link.service_name, "MemoryKeeper")
        self.assertIn("COMMON_FILE_CREATED", context.processing_log)
        self.assertIn("LINK_CREATED", context.processing_log)

    def test_duplicate_hash_for_another_service_creates_link_only(self) -> None:
        digest = "2" * 64
        file = self._file(digest)
        FileServiceRepository(self.session).ensure_link(
            file_id=file.id,
            service_name="MemoryKeeper",
        )

        with tempfile.TemporaryDirectory() as directory:
            incoming = Path(directory) / "astro.jpg"
            incoming.write_bytes(b"same bytes")
            job = UploadJob(
                job_id="b2000000-0000-0000-0000-000000000001",
                source_type="UPLOAD",
                status="PROCESSING",
                incoming_path="incoming/astro.jpg",
                service_name="AstroJournal",
            )
            storage = FakeStorageService(digest)
            context = PluginContext(
                db=self.session,
                storage_service=storage,
                incoming_path=incoming,
                job=job,
                service_name="AstroJournal",
            )
            HashPlugin().run(context)

        self.assertTrue(context.stop_pipeline)
        self.assertEqual(context.common_file.id, file.id)
        self.assertEqual(self.session.query(CommonFile).count(), 1)
        self.assertEqual(
            self.session.query(CommonFileService)
            .filter(CommonFileService.file_id == file.id)
            .count(),
            2,
        )
        self.assertIn("LINK_CREATED", context.processing_log)
        self.assertEqual(storage.deleted_paths, ["incoming/astro.jpg"])

    def test_same_service_duplicate_does_not_create_second_link(self) -> None:
        digest = "3" * 64
        file = self._file(digest)
        FileServiceRepository(self.session).ensure_link(
            file_id=file.id,
            service_name="MemoryKeeper",
        )
        existing_link_count = self.session.query(CommonFileService).count()

        with tempfile.TemporaryDirectory() as directory:
            incoming = Path(directory) / "again.jpg"
            incoming.write_bytes(b"same bytes")
            context = PluginContext(
                db=self.session,
                storage_service=FakeStorageService(digest),
                incoming_path=incoming,
                job=UploadJob(
                    job_id="b2000000-0000-0000-0000-000000000002",
                    source_type="UPLOAD",
                    status="PROCESSING",
                    incoming_path="incoming/again.jpg",
                ),
            )
            HashPlugin().run(context)

        self.assertEqual(self.session.query(CommonFileService).count(), existing_link_count)
        self.assertIn("LINK_EXISTS", context.processing_log)

    def test_gallery_filters_on_service_links_not_legacy_column(self) -> None:
        shared = self._file("4" * 64, service_name="MemoryKeeper")
        memory_only = self._file("5" * 64, service_name="MemoryKeeper")
        repository = FileServiceRepository(self.session)
        repository.ensure_link(file_id=shared.id, service_name="MemoryKeeper")
        repository.ensure_link(file_id=shared.id, service_name="AstroJournal")
        repository.ensure_link(file_id=memory_only.id, service_name="MemoryKeeper")

        gallery = GalleryService(self.session)
        astro = gallery.list_gallery(service_name="AstroJournal")
        memory = gallery.list_gallery(service_name="MemoryKeeper")

        self.assertEqual(astro.total, 1)
        self.assertEqual(astro.items[0].file_id, shared.file_id)
        self.assertEqual(astro.items[0].service_name, "AstroJournal")
        self.assertEqual(memory.total, 2)

    def test_schema_sync_backfills_legacy_service_link_idempotently(self) -> None:
        legacy_engine = create_engine("sqlite:///:memory:")
        try:
            CommonFile.__table__.create(legacy_engine)
            with legacy_engine.begin() as connection:
                connection.execute(
                    CommonFile.__table__.insert().values(
                        file_id="6" * 64,
                        original_name="legacy.jpg",
                        service_name="MemoryKeeper",
                    )
                )

            changes = initialize_database(legacy_engine)
            legacy_session = sessionmaker(bind=legacy_engine)()
            try:
                self.assertIn("backfill:common_file_services=1", changes)
                self.assertEqual(
                    legacy_session.query(CommonFileService)
                    .filter(CommonFileService.service_name == "MemoryKeeper")
                    .count(),
                    1,
                )
                self.assertEqual(initialize_database(legacy_engine), [])
            finally:
                legacy_session.close()
        finally:
            legacy_engine.dispose()
