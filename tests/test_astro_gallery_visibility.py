from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.common.model_registry import Base
from app.common.models.change_event import CommonChangeEvent
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.upload_job import UploadJob
from app.common.repositories.file_service_repository import FileServiceRepository
from app.common.services.changes_service import ChangesService
from app.astrojournal.models.observation_record import ObservationRecord
from app.astrojournal.schemas.observation_record import ObservationRecordCreate
from app.astrojournal.services.observation_record_service import ObservationRecordService
from app.astrojournal.services.gallery_visibility_service import AstroGalleryVisibilityService
from app.astrojournal.services.file_cleanup_service import AstroJournalFileCleanupService
from test_astrojournal_physical_delete import LocalStorageService
from worker.plugins.base import PluginContext
from worker.plugins.hash_plugin import HashPlugin
from worker.plugins.storage_plugin import StoragePlugin


class AstroGalleryVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.temp = tempfile.TemporaryDirectory()
        self.storage = LocalStorageService(Path(self.temp.name))
        self.incoming = self.storage.incoming_root / "restore.jpg"
        self.incoming.parent.mkdir(parents=True)
        self.incoming.write_bytes(b"local-test-asset")
        self.file = CommonFile(file_id=hashlib.sha256(self.incoming.read_bytes()).hexdigest(),
                               original_name="restore.jpg", deleted=False)
        self.db.add(self.file)
        self.db.flush()
        self.record = self.add_record()
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def add_record(self, *, service="AstroJournal", deleted=False):
        record = ObservationRecord(
            file_id=self.file.id, service_name=service,
            captured_at=datetime.now(timezone.utc), revision=5,
            deleted_at=datetime.now(timezone.utc) if deleted else None,
        )
        self.db.add(record)
        self.db.flush()
        return record

    def link(self):
        return FileServiceRepository(self.db).ensure_link(
            file_id=self.file.id, service_name="AstroJournal", commit=False)

    def context(self):
        job = UploadJob(job_id=str(uuid4()), source_type="UPLOAD", status="PROCESSING",
                        incoming_path=self.storage.to_relative_path(self.incoming),
                        service_name="AstroJournal")
        self.db.add(job)
        self.db.commit()
        return PluginContext(db=self.db, storage_service=self.storage,
                             incoming_path=self.incoming, job=job,
                             original_name="restore.jpg", extension=".jpg",
                             mime_type="image/jpeg", service_name="AstroJournal")

    def notifications(self):
        return self.db.query(CommonChangeEvent).filter_by(
            resource_type="AstroGalleryVisibility").order_by(CommonChangeEvent.id).all()

    def test_helper_batches_deduplicates_and_excludes_nonactive_records(self):
        second = self.add_record()
        self.add_record(service="MemoryKeeper")
        self.add_record(deleted=True)
        self.link()
        self.db.commit()
        statements = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().startswith("SELECT"):
                statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            ids = AstroGalleryVisibilityService(self.db).notify_for_files(
                [self.file.id, self.file.id])
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        self.assertEqual(set(ids), {self.record.id, second.id})
        self.assertEqual(len(statements), 1)
        self.assertEqual(len(self.notifications()), 2)
        self.assertEqual(self.record.revision, 5)
        self.db.rollback()
        self.assertEqual(self.notifications(), [])  # Helper never commits.

    def test_no_notification_without_complete_visibility(self):
        helper = AstroGalleryVisibilityService(self.db)
        self.assertEqual(helper.notify_for_files([]), [])
        self.assertEqual(helper.notify_for_files([self.file.id]), [])
        self.link()
        self.file.deleted = True
        self.assertEqual(helper.notify_for_files([self.file.id]), [])
        self.assertEqual(self.notifications(), [])

    def test_hash_plugin_recovers_link_and_emits_after_existing_cursor(self):
        context = self.context()
        HashPlugin().run(context)
        first_cursor = self.notifications()[0].id
        self.assertTrue(context.stop_pipeline)
        self.assertEqual(self.notifications()[0].revision, None)
        self.assertEqual(self.record.revision, 5)
        # Repeating ensure_link is not a new recovery notification.
        self.link()
        self.db.commit()
        self.assertEqual(len(self.notifications()), 1)
        FileServiceRepository(self.db).get(
            file_id=self.file.id, service_name="AstroJournal")
        self.db.query(CommonFileService).delete()
        self.db.commit()
        self.incoming.write_bytes(b"local-test-asset")
        HashPlugin().run(self.context())
        page = ChangesService(self.db).list_changes(cursor=first_cursor)
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].current_resource_state, "ACTIVE")
        self.assertEqual(page.items[0].current_revision, 5)
        self.assertGreater(page.next_cursor, first_cursor)

    def test_storage_restores_file_with_or_without_existing_link(self):
        for existing_link in (True, False):
            with self.subTest(existing_link=existing_link):
                self.incoming.write_bytes(b"local-test-asset")
                self.db.query(CommonFileService).delete()
                if existing_link:
                    self.link()
                self.file.deleted = True
                self.db.commit()
                before = len(self.notifications())
                context = self.context()
                HashPlugin().run(context)
                self.assertTrue(context.restore_deleted_common_file)
                StoragePlugin().run(context)
                self.db.refresh(self.file)
                self.assertFalse(self.file.deleted)
                self.assertIsNotNone(FileServiceRepository(self.db).get(
                    file_id=self.file.id, service_name="AstroJournal"))
                self.assertEqual(len(self.notifications()), before + 1)
                self.assertEqual(self.record.revision, 5)

    def test_storage_notification_failure_rolls_back_file_and_link_recovery(self):
        self.file.deleted = True
        self.db.commit()
        context = self.context()
        HashPlugin().run(context)
        notify = AstroGalleryVisibilityService.notify_for_files

        def append_then_fail(service, file_ids):
            notify(service, file_ids)
            raise RuntimeError("failure after event append")

        with patch.object(AstroGalleryVisibilityService, "notify_for_files",
                          autospec=True, side_effect=append_then_fail):
            with self.assertRaises(RuntimeError):
                StoragePlugin().run(context)
        self.db.refresh(self.file)
        self.assertTrue(self.file.deleted)
        self.assertEqual(self.notifications(), [])
        self.assertIsNone(FileServiceRepository(self.db).get(
            file_id=self.file.id, service_name="AstroJournal"))

    def test_record_create_recovers_link_for_existing_records(self):
        ObservationRecordService(self.db).create(ObservationRecordCreate(
            file_id=self.file.id, captured_at=datetime.now(timezone.utc)))
        self.assertEqual([e.resource_id for e in self.notifications()], [self.record.id])
        self.assertEqual(self.record.revision, 5)

    def test_enrichment_all_states_is_batch_and_keeps_event_revision_null(self):
        self.link()
        AstroGalleryVisibilityService(self.db).notify_for_files([self.file.id])
        self.db.commit()
        for state in ("ACTIVE", "UNAVAILABLE", "DELETED", "MISSING"):
            if state == "UNAVAILABLE":
                self.db.query(CommonFileService).delete()
            elif state == "DELETED":
                self.record.deleted_at = datetime.now(timezone.utc)
            elif state == "MISSING":
                self.db.delete(self.record)
            self.db.commit()
            page = ChangesService(self.db).list_changes()
            self.assertEqual(page.items[0].current_resource_state, state)
            self.assertIsNone(page.items[0].revision)
            self.assertFalse(page.items[0].tombstone)
            self.assertEqual(page.items[0].current_revision,
                             None if state == "MISSING" else 5)

    def test_export_actual_sqlite_recovery_pages_for_android_contract(self):
        """Optional cross-language fixture, produced by real SQLite mutations.

        No HTTP server, PostgreSQL or NAS is used. Android consumes these exact
        ChangesResponse JSON pages with a mocked detail transport.
        """
        self.file.deleted = True
        self.db.commit()
        context = self.context()
        HashPlugin().run(context)
        StoragePlugin().run(context)
        pages = []
        cursor = 0
        for stage in range(3):
            if stage:
                self.db.query(CommonFileService).delete()
                self.db.commit()
                self.incoming.write_bytes(b"local-test-asset")
                HashPlugin().run(self.context())
            page = ChangesService(self.db).list_changes(cursor=cursor)
            self.assertEqual(len(page.items), 1)
            self.assertEqual(page.items[0].current_resource_state, "ACTIVE")
            self.assertEqual(page.items[0].current_revision, 5)
            pages.append(page.model_dump(mode="json"))
            cursor = page.next_cursor
        ObservationRecordService(self.db, cleanup_service=AstroJournalFileCleanupService(
            self.db, storage_service=self.storage)).soft_delete(self.record.id)
        deleted_page = ChangesService(self.db).list_changes(cursor=cursor)
        self.assertEqual(deleted_page.items[0].operation, "DELETE")
        self.assertEqual(deleted_page.items[0].current_resource_state, "DELETED")
        pages.append(deleted_page.model_dump(mode="json"))
        destination = os.environ.get("ASTRO_VISIBILITY_FIXTURE_OUT")
        if destination:
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_text(json.dumps({
                "record_id": self.record.id, "pages": pages,
            }), encoding="utf-8")
