from __future__ import annotations

from datetime import datetime, timezone
import unittest
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.astrojournal.schemas.observation_record import (
    ObservationRecordCreate,
    ObservationRecordUpdate,
)
from app.astrojournal.services.observation_record_service import ObservationRecordService
from app.common.database import Base
from app.common.models.change_event import CommonChangeEvent
from app.common.models.file import CommonFile
from app.common.repositories.change_event_repository import (
    ChangeEventRepository,
    ChangeOperation,
)
from app.common.services.changes_service import ChangesService
from app.main import app


class SprintB601Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.records = ObservationRecordService(self.session)
        self.changes = ChangesService(self.session)
        self.file = CommonFile(file_id="d" * 64, original_name="changes.fits")
        self.session.add(self.file)
        self.session.commit()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _create(self, **overrides):
        values = {
            "file_id": self.file.id,
            "catalog_object_id": "M42",
            "captured_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
            **overrides,
        }
        return self.records.create(ObservationRecordCreate(**values))

    def test_create_update_delete_emit_ordered_events_and_tombstone(self) -> None:
        record = self._create()
        self.records.update(
            record.id,
            ObservationRecordUpdate(revision=1, memo="changed"),
        )
        self.records.soft_delete(record.id)

        response = self.changes.list_changes(service_name="AstroJournal")

        self.assertEqual(
            [item.operation for item in response.items],
            [ChangeOperation.CREATE, ChangeOperation.UPDATE, ChangeOperation.DELETE],
        )
        self.assertEqual([item.revision for item in response.items], [1, 2, 3])
        self.assertEqual(
            {item.resource_id for item in response.items},
            {record.id},
        )
        self.assertEqual(
            [item.tombstone for item in response.items],
            [False, False, True],
        )
        self.assertEqual(response.next_cursor, response.items[-1].cursor)
        self.assertFalse(response.has_more)

    def test_cursor_pagination_is_exclusive_and_stable(self) -> None:
        first = self._create(catalog_object_id="M1")
        second = self._create(catalog_object_id="M2")
        third = self._create(catalog_object_id="M3")

        page_one = self.changes.list_changes(cursor=0, limit=2)
        page_two = self.changes.list_changes(cursor=page_one.next_cursor, limit=2)

        self.assertEqual([item.resource_id for item in page_one.items], [first.id, second.id])
        self.assertTrue(page_one.has_more)
        self.assertEqual([item.resource_id for item in page_two.items], [third.id])
        self.assertFalse(page_two.has_more)
        self.assertGreater(page_two.next_cursor, page_one.next_cursor)

    def test_service_filter_only_returns_matching_events(self) -> None:
        astro = self._create()
        ChangeEventRepository(self.session).append(
            service_name="MemoryKeeper",
            resource_type="Photo",
            resource_id="memory-photo-1",
            operation=ChangeOperation.UPDATE,
            revision=4,
        )
        self.session.commit()

        astro_response = self.changes.list_changes(service_name="AstroJournal")
        memory_response = self.changes.list_changes(service_name="MemoryKeeper")

        self.assertEqual([item.resource_id for item in astro_response.items], [astro.id])
        self.assertEqual(
            [item.resource_id for item in memory_response.items],
            ["memory-photo-1"],
        )

    def test_idempotent_replay_and_stale_update_do_not_emit_events(self) -> None:
        client_record_id = uuid4()
        record = self._create(client_record_id=client_record_id)
        replay = self._create(client_record_id=client_record_id, memo="ignored")
        with self.assertRaises(HTTPException):
            self.records.update(
                record.id,
                ObservationRecordUpdate(revision=99, memo="stale"),
            )

        self.assertEqual(replay.id, record.id)
        self.assertEqual(self.session.query(CommonChangeEvent).count(), 1)

    def test_representative_demotion_emits_its_own_update_event(self) -> None:
        first = self._create(representative=True)
        second = self._create(representative=True)

        response = self.changes.list_changes(service_name="AstroJournal")
        projected = [
            (item.resource_id, item.operation, item.revision)
            for item in response.items
        ]

        self.assertEqual(
            projected,
            [
                (first.id, ChangeOperation.CREATE, 1),
                (first.id, ChangeOperation.UPDATE, 2),
                (second.id, ChangeOperation.CREATE, 1),
            ],
        )

    def test_schema_and_openapi_contract(self) -> None:
        inspector = inspect(self.engine)
        self.assertTrue(inspector.has_table("common_change_events"))
        index_names = {
            item["name"]
            for item in inspector.get_indexes("common_change_events")
        }
        self.assertIn("ix_common_change_events_service_cursor", index_names)

        operation = app.openapi()["paths"]["/api/common/changes"]["get"]
        parameters = {item["name"] for item in operation["parameters"]}
        self.assertEqual(parameters, {"cursor", "limit", "service_name"})
        self.assertIn("200", operation["responses"])
