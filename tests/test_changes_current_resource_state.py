from datetime import datetime, timezone
import unittest
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.astrojournal.models.observation_record import ObservationRecord
from app.astrojournal.services.gallery_service import AstroGalleryService
from app.common.model_registry import Base
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.repositories.change_event_repository import ChangeEventRepository
from app.common.schemas.changes import ChangeEventResponse
from app.common.services.changes_service import ChangesService


class ChangesCurrentResourceStateTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.events = ChangeEventRepository(self.db)
        self.changes = ChangesService(self.db)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def record(self, *, deleted=False, file_deleted=False, link=True,
               missing_file=False, service_name="AstroJournal"):
        file = CommonFile(file_id=uuid4().hex * 2, original_name="photo.jpg",
                          deleted=file_deleted)
        self.db.add(file)
        self.db.flush()
        record = ObservationRecord(
            file_id=file.id if not missing_file else -1,
            captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            service_name=service_name,
            revision=7,
            deleted_at=datetime.now(timezone.utc) if deleted else None,
        )
        self.db.add(record)
        if link:
            self.db.add(CommonFileService(file_id=file.id, service_name="AstroJournal"))
        self.db.flush()
        return record.id

    def append(self, record_id, operation="CREATE", *, service="AstroJournal",
               resource_type="ObservationRecord"):
        row = self.events.append(
            service_name=service, resource_type=resource_type,
            resource_id=record_id, operation=operation, revision=1,
            tombstone=operation == "DELETE",
        )
        return row.id

    def test_active_create_update_preserve_event_revision(self):
        record_id = self.record()
        for operation in ("CREATE", "UPDATE"):
            self.append(record_id, operation)
        self.db.commit()
        response = self.changes.list_changes(service_name="AstroJournal")
        self.assertEqual([item.current_resource_state for item in response.items],
                         ["ACTIVE", "ACTIVE"])
        self.assertEqual([item.current_revision for item in response.items], [7, 7])
        self.assertEqual([item.revision for item in response.items], [1, 1])
        self.assertEqual(str(AstroGalleryService(self.db).get_detail(record_id).record_id),
                         record_id)

    def test_deleted_history_and_tombstone_are_retained(self):
        record_id = self.record(deleted=True, file_deleted=True, link=False)
        for operation in ("CREATE", "UPDATE", "DELETE"):
            self.append(record_id, operation)
        self.db.commit()
        response = self.changes.list_changes()
        self.assertEqual([item.operation for item in response.items],
                         ["CREATE", "UPDATE", "DELETE"])
        self.assertEqual([item.current_resource_state for item in response.items],
                         ["DELETED"] * 3)
        self.assertEqual([item.tombstone for item in response.items], [False, False, True])
        self.assertEqual([item.current_revision for item in response.items], [7] * 3)

    def test_missing_record(self):
        self.append(str(uuid4()))
        self.db.commit()
        item = self.changes.list_changes().items[0]
        self.assertEqual(item.current_resource_state, "MISSING")
        self.assertIsNone(item.current_revision)

    def test_unavailable_matches_gallery_required_conditions(self):
        for options in (
            {"file_deleted": True}, {"link": False}, {"missing_file": True},
            {"service_name": "MemoryKeeper"},
        ):
            with self.subTest(options=options):
                record_id = self.record(**options)
                cursor = self.append(record_id)
                self.db.commit()
                item = self.changes.list_changes(cursor=cursor - 1).items[0]
                self.assertEqual(item.current_resource_state, "UNAVAILABLE")
                self.assertEqual(item.current_revision, 7)
                with self.assertRaises(HTTPException) as raised:
                    AstroGalleryService(self.db).get_detail(record_id)
                self.assertEqual(raised.exception.status_code, 404)

    def test_pages_preserve_order_cursors_and_all_states(self):
        active = self.record()
        deleted = self.record(deleted=True)
        unavailable = self.record(link=False)
        ids = [self.append(resource_id, operation) for resource_id, operation in (
            (active, "CREATE"), (deleted, "CREATE"), (deleted, "UPDATE"),
            (str(uuid4()), "UPDATE"), (unavailable, "CREATE"), (deleted, "DELETE"),
        )]
        self.db.commit()
        cursor, received, states = 0, [], []
        for expected_more in (True, True, False):
            page = self.changes.list_changes(cursor=cursor, limit=2)
            self.assertEqual(page.has_more, expected_more)
            self.assertGreater(page.next_cursor, cursor)
            self.assertEqual(page.next_cursor, page.items[-1].cursor)
            cursor = page.next_cursor
            received.extend(item.cursor for item in page.items)
            states.extend(item.current_resource_state for item in page.items)
        self.assertEqual(received, ids)
        self.assertEqual(states, ["ACTIVE", "DELETED", "DELETED", "MISSING",
                                  "UNAVAILABLE", "DELETED"])
        empty = self.changes.list_changes(cursor=cursor)
        self.assertEqual(empty.items, [])
        self.assertEqual(empty.next_cursor, cursor)
        self.assertFalse(empty.has_more)

    def test_page_uses_one_batch_query_not_one_query_per_event(self):
        for _ in range(12):
            record_id = self.record()
            self.append(record_id)
            self.append(record_id, "UPDATE")
        self.db.commit()
        selects = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                selects.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            response = self.changes.list_changes(limit=100)
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        self.assertEqual(len(response.items), 24)
        self.assertTrue(all(item.current_resource_state == "ACTIVE"
                            for item in response.items))
        self.assertEqual(len(selects), 2)  # Event page + one resource-state batch.

    def test_other_resources_and_services_are_not_enriched(self):
        record_id = self.record()
        self.append(record_id, service="MemoryKeeper")
        self.append(record_id, resource_type="Equipment")
        self.db.commit()
        selects = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            selects.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            response = self.changes.list_changes()
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        self.assertEqual(len(selects), 1)
        for item in response.items:
            self.assertIsNone(item.current_resource_state)
            self.assertIsNone(item.current_revision)

    def test_old_payload_remains_valid_and_old_fields_unchanged(self):
        record_id = self.record()
        cursor = self.append(record_id)
        self.db.commit()
        row = self.events.list_after(cursor=0, limit=100)[0][0]
        old_item = ChangeEventResponse.model_validate(row)
        new_item = self.changes.list_changes().items[0]
        additions = {"current_resource_state", "current_revision"}
        self.assertEqual(old_item.model_dump(exclude=additions),
                         new_item.model_dump(exclude=additions))
        self.assertEqual(new_item.cursor, cursor)
        legacy_payload = old_item.model_dump(by_alias=True, exclude=additions)
        legacy_payload["id"] = legacy_payload.pop("cursor")
        parsed = ChangeEventResponse.model_validate(legacy_payload)
        self.assertIsNone(parsed.current_resource_state)
