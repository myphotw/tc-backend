from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.astrojournal.models.observation_record import ObservationRecord
from app.astrojournal.schemas.observation_record import ObservationRecordCreate
from app.astrojournal.services.file_cleanup_service import (
    AstroJournalFileCleanupService,
    FileCleanupStatus,
)
from app.astrojournal.services.gallery_service import AstroGalleryService
from app.astrojournal.services.observation_record_service import ObservationRecordService
from app.common.database import Base
from app.common.models.change_event import CommonChangeEvent
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.file_tag import CommonFileTag
from app.common.models.metadata_history import CommonMetadataHistory
from app.common.models.upload_job import UploadJob
from app.common.models.vision_job import CommonVisionJob
from app.common.repositories.vision_job_repository import VisionJobStatus
from app.common.services.changes_service import ChangesService
from app.common.services.storage_service import AssetDeleteStatus, StorageService
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
    def original_root(self) -> Path:
        return self.root / "original"

    @property
    def preview_root(self) -> Path:
        return self.root / "preview"

    @property
    def thumb_root(self) -> Path:
        return self.root / "thumb"


class AstroJournalPhysicalDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = LocalStorageService(Path(self.temp.name) / "PhotoPlatform")
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.cleanup = AstroJournalFileCleanupService(
            self.session,
            storage_service=self.storage,
        )
        self.records = ObservationRecordService(
            self.session,
            cleanup_service=self.cleanup,
        )
        self.file_counter = 0

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()
        self.temp.cleanup()

    def _create_file(self) -> tuple[CommonFile, dict[str, Path]]:
        self.file_counter += 1
        digest = f"{self.file_counter:064x}"
        paths = {
            "original": self.storage.original_root / "AstroJournal" / "2026" / "M42" / f"{digest}.jpg",
            "preview": self.storage.preview_root / digest[:2] / digest[2:4] / f"{digest}.jpg",
            "thumb": self.storage.thumb_root / digest[:2] / digest[2:4] / f"{digest}.jpg",
        }
        for kind, path in paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(kind.encode("ascii"))

        common_file = CommonFile(
            file_id=digest,
            original_name="astro.jpg",
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
        self.session.add(
            CommonFileService(file_id=common_file.id, service_name="AstroJournal")
        )
        self.session.commit()
        return common_file, paths

    def _create_record(self, common_file: CommonFile) -> ObservationRecord:
        return self.records.create(
            ObservationRecordCreate(
                file_id=common_file.id,
                catalog_object_id="M42",
                captured_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
            )
        )

    def test_last_astro_record_deletes_assets_and_tombstones_common_file(self) -> None:
        common_file, paths = self._create_file()
        record = self._create_record(common_file)
        self.session.add_all(
            [
                CommonFileMetadata(file_id=common_file.id),
                CommonFileTag(
                    file_id=common_file.id,
                    tag="nebula",
                    tag_type="AI",
                    source="AI",
                    deleted=False,
                ),
                CommonMetadataHistory(
                    file_id=common_file.id,
                    field_name="memo",
                    new_value="history",
                    source="SYSTEM",
                    priority=1,
                ),
                CommonVisionJob(
                    file_id=common_file.id,
                    priority=0,
                    status=VisionJobStatus.COMPLETED,
                    vision_provider="GOOGLE",
                    deleted=False,
                ),
            ]
        )
        self.session.commit()

        deleted = self.records.soft_delete(record.id)

        self.assertIsNotNone(deleted.deleted_at)
        self.assertEqual(self.records.last_cleanup_result.status, FileCleanupStatus.CLEANED)
        self.assertTrue(self.records.last_cleanup_result.physical_file_deleted)
        self.assertTrue(all(not path.exists() for path in paths.values()))
        self.session.refresh(common_file)
        self.assertTrue(common_file.deleted)
        self.assertIsNone(common_file.original_path)
        self.assertIsNone(common_file.preview_path)
        self.assertIsNone(common_file.thumb_path)
        self.assertEqual(self.session.query(CommonFileService).count(), 0)
        self.assertEqual(self.session.query(CommonFileMetadata).count(), 0)
        self.assertEqual(self.session.query(CommonFileTag).count(), 0)
        self.assertTrue(self.session.query(CommonVisionJob).one().deleted)
        self.assertEqual(self.session.query(CommonMetadataHistory).count(), 1)
        self.assertEqual(AstroGalleryService(self.session).list_gallery().total, 0)

        event_count = self.session.query(CommonChangeEvent).count()
        replay = self.records.soft_delete(record.id)
        self.assertEqual(replay.revision, deleted.revision)
        self.assertEqual(
            self.records.last_cleanup_result.status,
            FileCleanupStatus.ALREADY_CLEANED,
        )
        self.assertEqual(self.session.query(CommonChangeEvent).count(), event_count)

    def test_multiple_astro_records_preserve_until_last_delete(self) -> None:
        common_file, paths = self._create_file()
        first = self._create_record(common_file)
        second = self._create_record(common_file)

        self.records.soft_delete(first.id)
        self.assertEqual(
            self.records.last_cleanup_result.status,
            FileCleanupStatus.PRESERVED_ACTIVE_RECORD,
        )
        self.assertTrue(all(path.exists() for path in paths.values()))
        self.assertFalse(common_file.deleted)

        self.records.soft_delete(second.id)
        self.assertEqual(self.records.last_cleanup_result.status, FileCleanupStatus.CLEANED)
        self.assertTrue(all(not path.exists() for path in paths.values()))

    def test_memorykeeper_link_preserves_assets_and_removes_only_astro_link(self) -> None:
        common_file, paths = self._create_file()
        record = self._create_record(common_file)
        self.session.add(
            CommonFileService(file_id=common_file.id, service_name="MemoryKeeper")
        )
        self.session.commit()

        self.records.soft_delete(record.id)

        self.assertEqual(
            self.records.last_cleanup_result.status,
            FileCleanupStatus.PRESERVED_OTHER_SERVICE,
        )
        self.assertTrue(all(path.exists() for path in paths.values()))
        self.session.refresh(common_file)
        self.assertFalse(common_file.deleted)
        links = {
            link.service_name
            for link in self.session.query(CommonFileService).filter_by(file_id=common_file.id)
        }
        self.assertEqual(links, {"MemoryKeeper"})

    def test_missing_and_partial_assets_are_idempotent_success(self) -> None:
        common_file, paths = self._create_file()
        record = self._create_record(common_file)
        paths["original"].unlink()
        paths["thumb"].unlink()

        self.records.soft_delete(record.id)

        result = self.records.last_cleanup_result
        self.assertEqual(result.status, FileCleanupStatus.CLEANED)
        self.assertEqual(result.asset_results["original"], AssetDeleteStatus.ALREADY_ABSENT)
        self.assertEqual(result.asset_results["preview"], AssetDeleteStatus.DELETED)
        self.assertEqual(result.asset_results["thumb"], AssetDeleteStatus.ALREADY_ABSENT)
        self.assertFalse(paths["preview"].exists())
        self.assertTrue(common_file.deleted)

    def test_outside_path_is_rejected_before_any_asset_is_deleted(self) -> None:
        common_file, paths = self._create_file()
        record = self._create_record(common_file)
        outside = Path(self.temp.name) / "outside.jpg"
        outside.write_bytes(b"do-not-delete")
        common_file.original_path = str(outside.resolve())
        self.session.commit()

        self.records.soft_delete(record.id)

        result = self.records.last_cleanup_result
        self.assertEqual(result.status, FileCleanupStatus.ASSET_DELETE_FAILED)
        self.assertEqual(result.asset_results["original"], AssetDeleteStatus.UNSAFE_PATH)
        self.assertEqual(result.asset_results["preview"], AssetDeleteStatus.NOT_ATTEMPTED)
        self.assertTrue(outside.exists())
        self.assertTrue(paths["preview"].exists())
        self.assertTrue(paths["thumb"].exists())
        self.session.refresh(common_file)
        self.assertFalse(common_file.deleted)
        self.assertIsNotNone(common_file.original_path)

        common_file.original_path = self.storage.to_relative_path(paths["original"])
        self.session.commit()
        event_count = self.session.query(CommonChangeEvent).count()
        self.records.soft_delete(record.id)
        self.assertEqual(self.records.last_cleanup_result.status, FileCleanupStatus.CLEANED)
        self.assertTrue(all(not path.exists() for path in paths.values()))
        self.assertTrue(outside.exists())
        self.assertEqual(self.session.query(CommonChangeEvent).count(), event_count)

    def test_processing_vision_job_is_a_temporary_preservation_reference(self) -> None:
        common_file, paths = self._create_file()
        record = self._create_record(common_file)
        vision_job = CommonVisionJob(
            file_id=common_file.id,
            priority=0,
            status=VisionJobStatus.PROCESSING,
            vision_provider="GOOGLE",
            deleted=False,
        )
        self.session.add(vision_job)
        self.session.commit()

        self.records.soft_delete(record.id)

        self.assertEqual(
            self.records.last_cleanup_result.status,
            FileCleanupStatus.PRESERVED_PROCESSING_VISION,
        )
        self.assertTrue(all(path.exists() for path in paths.values()))
        self.assertFalse(common_file.deleted)

    def test_gallery_and_changes_keep_existing_delete_projection(self) -> None:
        common_file, _ = self._create_file()
        record = self._create_record(common_file)

        self.records.soft_delete(record.id)

        self.assertEqual(AstroGalleryService(self.session).list_gallery().total, 0)
        changes = ChangesService(self.session).list_changes(service_name="AstroJournal")
        self.assertEqual([item.operation for item in changes.items], ["CREATE", "DELETE"])
        self.assertFalse(changes.items[0].tombstone)
        self.assertTrue(changes.items[1].tombstone)
        self.assertEqual(changes.items[1].resource_id, record.id)

    def test_reupload_restores_common_file_tombstone_instead_of_false_dedup(self) -> None:
        incoming = self.storage.storage_root / "incoming" / "restored.jpg"
        incoming.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (20, 10), color="navy").save(incoming, format="JPEG")
        digest = hashlib.sha256(incoming.read_bytes()).hexdigest()
        common_file = CommonFile(
            file_id=digest,
            original_name="deleted.jpg",
            service_name="AstroJournal",
            deleted=True,
        )
        job = UploadJob(
            job_id="c1000000-0000-0000-0000-000000000001",
            source_type="UPLOAD",
            status="PROCESSING",
            incoming_path=self.storage.to_relative_path(incoming),
            service_name="AstroJournal",
        )
        self.session.add_all([common_file, job])
        self.session.commit()
        original_id = common_file.id
        context = PluginContext(
            db=self.session,
            storage_service=self.storage,
            incoming_path=incoming,
            job=job,
            original_name="restored.jpg",
            extension=".jpg",
            mime_type="image/jpeg",
            service_name="AstroJournal",
            metadata={"observation_date": "2026-08-18", "canonical_target_id": "M42"},
        )

        HashPlugin().run(context)
        self.assertTrue(context.restore_deleted_common_file)
        self.assertFalse(context.stop_pipeline)
        PreviewPlugin().run(context)
        StoragePlugin().run(context)

        self.session.refresh(common_file)
        self.assertEqual(common_file.id, original_id)
        self.assertFalse(common_file.deleted)
        self.assertIsNotNone(common_file.original_path)
        self.assertTrue(self.storage.resolve_storage_path(common_file.original_path).exists())
        self.assertTrue(self.storage.resolve_storage_path(common_file.preview_path).exists())
        self.assertTrue(self.storage.resolve_storage_path(common_file.thumb_path).exists())
        self.assertEqual(
            self.session.query(CommonFileService)
            .filter_by(file_id=common_file.id, service_name="AstroJournal")
            .count(),
            1,
        )
        self.assertIn("DELETED_FILE_REUPLOAD", context.processing_log)
        self.assertIn("COMMON_FILE_RESTORED", context.processing_log)


if __name__ == "__main__":
    unittest.main()
